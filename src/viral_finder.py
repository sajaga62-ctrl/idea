"""Free ($0) viral AI-demo link finder for X (Twitter).

Discovers *candidate* public X post URLs via the free Exa search API (primary),
manual ingestion, and a best-effort free aggregator, then **verifies** each
candidate with unauthenticated, permitted metadata: yt-dlp's public X/Twitter
extractor and a ranged HTTP check against the video CDN.

Only posts that satisfy *every* qualifying condition are written to
``viral-links/YYYY-MM-DD.json``:

  1. published within the rolling time window (default 24h),
  2. at least ``min_views`` views (default 50,000),
  3. AI relevance (frontier model / agent / notable capability),
  4. a genuine, playable video stream,
  5. a verifiable public URL and metadata,
  6. a publicly accessible video (verified without bypassing restrictions).

No paid API, no credentials, no CAPTCHA/anti-bot bypassing, no fabricated
metadata. Fields that cannot be verified cause the candidate to be excluded.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import yt_dlp

from src.downloader import extract_post_id, validate_x_url
from src.exa_search import (
    DEFAULT_EXA_NUM_RESULTS,
    DEFAULT_PER_RUN_QUERY_CAP,
    DEFAULT_TOPICS,
    ExaSearchProvider,
    SearchProvider,
    SearchProviderError,
    SearchProviderExhaustedError,
    SearchProviderRateLimitError,
    build_keyword_templates,
    parse_search_keywords,
)

DEFAULT_MIN_VIEWS = 50_000
DEFAULT_HOURS = 24
DEFAULT_MAX_CANDIDATES = 25
DEFAULT_OUTPUT_DIR = "viral-links"
DEFAULT_RETENTION_DAYS = 30

# Evidence provenance recorded on each verified record.
EVIDENCE_SOURCE = (
    "yt-dlp twitter extractor (unauthenticated public metadata) + "
    "ranged HTTP check on video CDN"
)

# Accepted canonical domains (mirrors src.downloader.ACCEPTED_DOMAINS).
_X_HOST = r"(?:www\.|mobile\.)?(?:x|twitter)\.com"
# Matches /<user>/status/<id> and /i/web/status/<id>, optionally with trailing
# path segments (e.g. /video/1) or query strings.
_STATUS_RE = re.compile(
    rf"{_X_HOST}/(?:i/web/status|(?:[A-Za-z0-9_]{{1,20}})/status(?:es)?)/(\d+)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# AI relevance taxonomy
# ---------------------------------------------------------------------------

# Broad AI signals. Matched on word boundaries so short tokens like "ai" or
# "ml" do not match inside unrelated words (e.g. "email", "html").
AI_RELEVANCE_TERMS: Tuple[str, ...] = (
    "ai",
    "artificial intelligence",
    "machine learning",
    "deep learning",
    "neural network",
    "llm",
    "large language model",
    "foundation model",
    "frontier model",
    "genai",
    "generative",
    "gpt",
    "chatgpt",
    "openai",
    "anthropic",
    "claude",
    "gemini",
    "deepmind",
    "llama",
    "mistral",
    "grok",
    "qwen",
    "deepseek",
    "copilot",
    "agent",
    "agentic",
    "coding agent",
    "computer use",
    "computer-use",
    "multimodal",
    "vision model",
    "diffusion",
    "video generation",
    "text to video",
    "text-to-video",
    "sora",
    "veo",
    "runway",
    "midjourney",
    "robot",
    "robotics",
    "humanoid",
    "embodied",
    "transformer",
    "inference",
    "fine-tune",
    "fine-tuned",
    "self driving",
    "self-driving",
    "autonomous",
)

# Ordered topic taxonomy used to populate ``model_or_topic``. The first entry
# whose pattern matches the post text wins, so list specific model names first.
TOPIC_PATTERNS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("OpenAI GPT", ("gpt-5", "gpt-4", "gpt4", "gpt", "chatgpt", "openai")),
    ("Anthropic Claude", ("claude", "anthropic")),
    ("Google Gemini", ("gemini", "deepmind")),
    ("xAI Grok", ("grok",)),
    ("Meta Llama", ("llama",)),
    ("Mistral", ("mistral",)),
    ("DeepSeek", ("deepseek",)),
    ("Qwen", ("qwen",)),
    ("Microsoft Copilot", ("copilot",)),
    ("OpenAI Sora", ("sora",)),
    ("Google Veo", ("veo",)),
    ("Runway", ("runway",)),
    ("Midjourney", ("midjourney",)),
    ("Stable Diffusion", ("stable diffusion",)),
    ("AI coding agent", ("coding agent", "coder", "codex", "devin")),
    ("Computer-use agent", ("computer use", "computer-use", "browser agent")),
    ("AI agent", ("ai agent", "agentic", "agent")),
    ("Robotics", ("robot", "robotics", "humanoid", "embodied")),
    ("Video generation", ("video generation", "text-to-video", "text to video")),
    ("Multimodal AI", ("multimodal", "vision model")),
    ("Automotive AI", ("self-driving", "self driving")),
)


def _term_regex(term: str) -> re.Pattern:
    """Builds a word-boundary, case-insensitive regex for a taxonomy term."""
    escaped = re.escape(term)
    # Allow hyphen/space interchangeability for multi-word terms.
    escaped = escaped.replace(r"\ ", r"[\s\-]+").replace(r"\-", r"[\s\-]+")
    return re.compile(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", re.IGNORECASE)


_RELEVANCE_REGEXES = tuple(_term_regex(t) for t in AI_RELEVANCE_TERMS)


def is_ai_related(text: str) -> bool:
    """Returns True if ``text`` references a frontier AI model or capability."""
    if not text or not isinstance(text, str):
        return False
    return any(rx.search(text) for rx in _RELEVANCE_REGEXES)


def detect_topic(text: str) -> str:
    """Best-effort, text-derived topic label for a post.

    Never invents a model: falls back to ``"AI (unspecified)"`` when the post
    is AI-related but no specific topic keyword is present.
    """
    if text:
        for label, patterns in TOPIC_PATTERNS:
            for pattern in patterns:
                if _term_regex(pattern).search(text):
                    return label
    return "AI (unspecified)"


# ---------------------------------------------------------------------------
# URL normalisation + de-duplication
# ---------------------------------------------------------------------------


def normalize_post_url(raw: object) -> Optional[str]:
    """Normalises any X/Twitter status reference to a canonical HTTPS URL.

    Accepts bare URLs, URLs embedded in text, ``twitter.com`` aliases, ``www``
    and ``mobile`` subdomains, ``/statuses/`` legacy paths, ``/i/web/status/``
    links and trailing path/query segments. Returns ``None`` when no valid
    numeric status id is present.
    """
    if not raw or not isinstance(raw, str):
        return None

    # Undo HTML escaping that commonly appears in scraped/aggregator text.
    candidate = raw.replace("&amp;", "&").strip()

    match = _STATUS_RE.search(candidate)
    if match:
        post_id = match.group(1)
        host_match = re.search(_X_HOST, candidate, re.IGNORECASE)
        host = (host_match.group(0) if host_match else "x.com").lower()
        host = host.replace("mobile.", "").replace("www.", "")
        if host == "twitter.com":
            host = "x.com"
        user_match = re.search(
            rf"{_X_HOST}/([A-Za-z0-9_]{{1,20}})/status", candidate, re.IGNORECASE
        )
        user = user_match.group(1) if user_match else "i/web"
        canonical = f"https://{host}/{user}/status/{post_id}"
    else:
        # Fall back to the shared downloader parser for anything else.
        post_id = extract_post_id(candidate)
        if not post_id:
            return None
        canonical = f"https://x.com/i/web/status/{post_id}"

    # Confirm the canonical URL is accepted by the existing downloader parser.
    if extract_post_id(canonical) != post_id:
        return None
    return canonical


def extract_candidate_urls(text: str) -> List[str]:
    """Extracts and normalises every distinct X status URL found in ``text``."""
    found: List[str] = []
    seen = set()
    for match in _STATUS_RE.finditer(text or ""):
        normalized = normalize_post_url(match.group(0))
        if normalized and normalized not in seen:
            seen.add(normalized)
            found.append(normalized)
    return found


# Exa cannot index x.com status pages directly, but aggregator/mirror/video-embed
# sites expose the numeric post id in their own paths, and those pages ARE
# indexed. We extract those ids here and verify them against the real canonical
# X URL, so a mirror hit becomes a candidate post link.
_MIRROR_ID_PATTERNS = (
    re.compile(r"/status(?:es)?/(\d{15,25})", re.IGNORECASE),
    re.compile(r"-tw-(\d{15,25})", re.IGNORECASE),
    re.compile(r"/videos?/(\d{15,25})", re.IGNORECASE),
    re.compile(r"/(?:post|posts|p)/(\d{15,25})", re.IGNORECASE),
)


def extract_mirror_status_ids(url: str) -> List[str]:
    """Extracts embedded X post ids from mirror/aggregator URLs."""
    if not url or not isinstance(url, str):
        return []
    ids: List[str] = []
    for pattern in _MIRROR_ID_PATTERNS:
        for match in pattern.finditer(url):
            ids.append(match.group(1))
    return list(dict.fromkeys(ids))


def candidate_urls_from_search_hit(url: str) -> Tuple[List[str], str]:
    """Turns one search hit into canonical X post URLs.

    Returns ``(urls, kind)`` where ``kind`` is ``"direct"``, ``"mirror"`` or
    ``"none"``. A direct x.com/twitter.com status URL wins; otherwise an id
    embedded in a mirror/aggregator path is used and verified on the real post.
    """
    direct = normalize_post_url(url)
    if direct:
        return [direct], "direct"
    ids = extract_mirror_status_ids(url)
    if ids:
        return [f"https://x.com/i/web/status/{post_id}" for post_id in ids], "mirror"
    return [], "none"


def dedupe_candidates(urls: Iterable[object]) -> List[str]:
    """Normalises and de-duplicates candidate URLs, preserving first-seen order."""
    result: List[str] = []
    seen = set()
    for raw in urls:
        normalized = normalize_post_url(raw)
        if not normalized:
            continue
        post_id = extract_post_id(normalized)
        if post_id and post_id not in seen:
            seen.add(post_id)
            result.append(normalized)
    return result


# ---------------------------------------------------------------------------
# Qualifying-condition helpers
# ---------------------------------------------------------------------------


def parse_published_at(timestamp: object) -> Optional[datetime]:
    """Converts an epoch timestamp (int/float/str) to an aware UTC datetime."""
    if timestamp is None:
        return None
    try:
        seconds = float(timestamp)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def within_window(published_at: Optional[datetime], now: datetime, hours: int) -> bool:
    """True when ``published_at`` is inside the rolling ``hours`` window ending now."""
    if published_at is None or now is None:
        return False
    if published_at.tzinfo is None or now.tzinfo is None:
        return False
    if published_at > now + timedelta(minutes=5):
        # Allow a small clock-skew tolerance but reject future-dated posts.
        return False
    return published_at >= now - timedelta(hours=hours)


def meets_view_threshold(views: object, min_views: int) -> bool:
    """True when ``views`` is a non-negative integer >= ``min_views``."""
    if views is None or isinstance(views, bool):
        return False
    try:
        count = int(views)
    except (TypeError, ValueError):
        return False
    return count >= min_views


def video_is_present(info: Dict) -> bool:
    """True when yt-dlp exposed at least one real video stream."""
    formats = info.get("formats") or []
    has_format = any(f.get("url") for f in formats)
    duration = info.get("duration")
    return bool(has_format) and isinstance(duration, (int, float)) and duration > 0


def select_best_format(info: Dict) -> Optional[Dict]:
    """Picks the highest-quality downloadable format from yt-dlp metadata."""
    formats = [f for f in (info.get("formats") or []) if f.get("url")]
    if not formats:
        return None

    def sort_key(fmt: Dict):
        return (
            fmt.get("height") or 0,
            fmt.get("tbr") or 0,
            fmt.get("filesize") or 0,
        )

    return max(formats, key=sort_key)


# ---------------------------------------------------------------------------
# Metadata retrieval (unauthenticated, permitted)
# ---------------------------------------------------------------------------


class CapabilityUnavailableError(Exception):
    """Raised when the environment cannot reach the required public services."""


class MetadataError(Exception):
    """Raised when a post's metadata cannot be read (no/!available video)."""


_NETWORK_ERROR_MARKERS = (
    "timed out",
    "timeout",
    "connection",
    "temporary failure",
    "getaddrinfo",
    "failed to resolve",
    "network is unreachable",
    "ssl",
    "connection reset",
)


def _looks_like_network_error(message: str) -> bool:
    lowered = (message or "").lower()
    return any(marker in lowered for marker in _NETWORK_ERROR_MARKERS)


def fetch_post_metadata(post_url: str) -> Dict:
    """Reads public post metadata via yt-dlp without downloading the video.

    Returns a dict with ``post_id``, ``post_text``, ``published_at`` (aware
    datetime or None), ``views`` (int or None) and the raw ``info`` payload.

    Raises:
        CapabilityUnavailableError: on connectivity failures.
        MetadataError: when the post has no accessible public video.
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(post_url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        message = str(exc)
        if _looks_like_network_error(message):
            raise CapabilityUnavailableError(message) from exc
        raise MetadataError(message) from exc
    except (OSError, urllib.error.URLError) as exc:  # pragma: no cover - network
        raise CapabilityUnavailableError(str(exc)) from exc

    if not info:
        raise MetadataError("no metadata returned")

    post_text = info.get("description") or info.get("title") or ""
    post_id = str(info.get("id") or "") or extract_post_id(post_url) or ""
    return {
        "post_id": post_id,
        "post_text": post_text,
        "published_at": parse_published_at(info.get("timestamp")),
        "views": info.get("view_count"),
        "info": info,
    }


def check_video_accessible(info: Dict, timeout: int = 20) -> bool:
    """Confirms a video stream is publicly fetchable via a ranged HTTP request.

    Downloads only the first kilobyte; never retrieves the whole file and never
    bypasses any access control.
    """
    fmt = select_best_format(info)
    if not fmt:
        return False

    request = urllib.request.Request(
        fmt["url"],
        headers={"Range": "bytes=0-1023", "User-Agent": "Mozilla/5.0 (compatible)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if not (200 <= response.status < 300):
                return False
            content_type = (response.headers.get("Content-Type") or "").lower()
            chunk = response.read(64)
    except (urllib.error.URLError, OSError, ValueError):
        return False

    if not chunk:
        return False
    return content_type.startswith("video/") or content_type in (
        "application/octet-stream",
        "binary/octet-stream",
    )


# ---------------------------------------------------------------------------
# Record construction + verification pipeline
# ---------------------------------------------------------------------------


def build_record(
    post_url: str,
    metadata: Dict,
    *,
    min_views: int,
    hours: int,
    now: datetime,
    video_accessible: bool,
) -> Tuple[Optional[Dict], str]:
    """Turns fetched metadata into a qualifying record, or an exclusion reason."""
    post_id = str(metadata.get("post_id") or "") or (extract_post_id(post_url) or "")
    if not post_id:
        return None, "missing post id"

    info = metadata.get("info") or {}

    if not video_is_present(info):
        return None, "no genuine video stream found"

    published_at = metadata.get("published_at")
    if not isinstance(published_at, datetime):
        return None, "publication timestamp could not be verified"
    if not within_window(published_at, now, hours):
        return None, f"published outside the rolling {hours}h window"

    views = metadata.get("views")
    if not meets_view_threshold(views, min_views):
        return None, f"views below the {min_views:,} threshold (or unverified)"

    post_text = metadata.get("post_text") or ""
    if not is_ai_related(post_text):
        return None, "not verifiably related to a frontier AI model or capability"

    if not video_accessible:
        return None, "video stream not publicly accessible"

    discovered_at = now.astimezone(timezone.utc)
    record = {
        "post_url": post_url,
        "post_id": post_id,
        "post_text": post_text,
        "published_at": published_at.astimezone(timezone.utc).isoformat(),
        "views": int(views),
        "model_or_topic": detect_topic(post_text),
        "video_present": True,
        "video_accessible": True,
        "verification_status": "verified",
        "evidence_source": EVIDENCE_SOURCE,
        "discovered_at": discovered_at.isoformat(),
    }
    return record, "verified"


def verify_candidate(
    post_url: str,
    *,
    min_views: int,
    hours: int,
    now: datetime,
    metadata_fetcher: Callable[[str], Dict] = fetch_post_metadata,
    accessibility_checker: Callable[[Dict], bool] = check_video_accessible,
) -> Tuple[Optional[Dict], str]:
    """Fetches and verifies one candidate; returns (record|None, reason)."""
    normalized = normalize_post_url(post_url)
    if not normalized:
        return None, "unrecognised post URL"

    try:
        validate_x_url(normalized)
    except Exception as exc:  # pragma: no cover - defensive
        return None, f"invalid post URL: {exc}"

    try:
        metadata = metadata_fetcher(normalized)
    except CapabilityUnavailableError:
        raise
    except MetadataError as exc:
        return None, f"metadata unavailable: {exc}"
    except Exception as exc:  # noqa: BLE001 - surface any extractor error honestly
        message = str(exc)
        if _looks_like_network_error(message):
            raise CapabilityUnavailableError(message) from exc
        return None, f"metadata unavailable: {message}"

    info = metadata.get("info") or {}
    accessible = bool(video_is_present(info)) and accessibility_checker(info)
    return build_record(
        normalized,
        metadata,
        min_views=min_views,
        hours=hours,
        now=now,
        video_accessible=accessible,
    )


def verify_candidates(
    candidates: Sequence[str],
    *,
    min_views: int = DEFAULT_MIN_VIEWS,
    hours: int = DEFAULT_HOURS,
    now: Optional[datetime] = None,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    metadata_fetcher: Callable[[str], Dict] = fetch_post_metadata,
    accessibility_checker: Callable[[Dict], bool] = check_video_accessible,
    log: Callable[[str], None] = print,
) -> Tuple[List[Dict], List[Tuple[str, str]]]:
    """Verifies candidates (deduped, capped) and returns (records, rejections)."""
    now = now or datetime.now(timezone.utc)
    records: List[Dict] = []
    rejections: List[Tuple[str, str]] = []
    seen_ids = set()

    for candidate in dedupe_candidates(candidates):
        if len(seen_ids) >= max_candidates:
            break
        post_id = extract_post_id(candidate)
        if post_id in seen_ids:
            continue
        seen_ids.add(post_id)

        record, reason = verify_candidate(
            candidate,
            min_views=min_views,
            hours=hours,
            now=now,
            metadata_fetcher=metadata_fetcher,
            accessibility_checker=accessibility_checker,
        )
        if record:
            records.append(record)
            log(f"[QUALIFIED] {candidate} ({record['views']:,} views)")
        else:
            rejections.append((candidate, reason))
            log(f"[EXCLUDED]  {candidate} -> {reason}")

    return records, rejections


# ---------------------------------------------------------------------------
# Best-effort free discovery source (Hacker News / Algolia)
# ---------------------------------------------------------------------------

HN_ALGOLIA_ENDPOINT = "https://hn.algolia.com/api/v1/search_by_date"


def discover_from_hackernews(
    query: str,
    *,
    hours: int = DEFAULT_HOURS,
    limit: int = 100,
    now: Optional[datetime] = None,
    timeout: int = 20,
) -> List[str]:
    """Best-effort discovery of candidate X links shared on Hacker News.

    Uses the free, key-less Algolia HN API. Yields candidate *links only* (never
    metadata) and is deliberately conservative: it scans recent comments and
    stories for embedded ``x.com``/``twitter.com`` status URLs. Observed yield
    in practice is near-zero, so this is a supplementary source, not a primary
    one (see docs/x-browser-discovery.md).
    """
    now = now or datetime.now(timezone.utc)
    since = int((now - timedelta(hours=hours)).timestamp())
    found: List[str] = []

    for tag in ("comment", "story"):
        params = urllib.parse.urlencode(
            {
                "query": query,
                "tags": tag,
                "hitsPerPage": min(limit, 100),
                "numericFilters": f"created_at_i>{since}",
            }
        )
        request = urllib.request.Request(
            f"{HN_ALGOLIA_ENDPOINT}?{params}",
            headers={"User-Agent": "viral-x-finder/1.0 (+free-tier)"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
            continue

        for hit in payload.get("hits", []):
            blob = " ".join(
                str(hit.get(key) or "")
                for key in ("comment_text", "story_text", "story_title", "title", "url")
            )
            found.extend(extract_candidate_urls(blob))

    return dedupe_candidates(found)


# ---------------------------------------------------------------------------
# Primary discovery source (Exa, free tier)
# ---------------------------------------------------------------------------


@dataclass
class DiscoveryStats:
    """Observability for one discovery run (provider, hit rate, cost)."""

    provider: str
    queries_run: int = 0
    results_total: int = 0
    direct_hits: int = 0
    mirror_hits: int = 0
    non_status_discarded: int = 0
    cost_usd: float = 0.0
    notes: List[str] = field(default_factory=list)

    @property
    def status_hits(self) -> int:
        return self.direct_hits + self.mirror_hits

    @property
    def status_hit_rate(self) -> float:
        if self.results_total == 0:
            return 0.0
        return self.status_hits / self.results_total


def discover_with_exa(
    provider: SearchProvider,
    keywords: Sequence[str] = DEFAULT_TOPICS,
    *,
    max_queries: int = DEFAULT_PER_RUN_QUERY_CAP,
    num_results: int = DEFAULT_EXA_NUM_RESULTS,
    start_published_date: Optional[str] = None,
    log: Callable[[str], None] = print,
) -> Tuple[List[str], DiscoveryStats]:
    """Discovers candidate X post URLs for the supplied search keywords.

    The keywords drive the search: each term is queried **verbatim** plus a
    same-text domain-filtered variant and one source-hinted variant needed to
    surface X posts via mirror pages. Returns de-duplicated candidate URLs plus
    :class:`DiscoveryStats`. Non-status results are discarded and counted.
    Provider exhaustion or rate limiting stops the run gracefully instead of
    retrying aggressively.
    """
    stats = DiscoveryStats(provider=getattr(provider, "name", "unknown"))

    if not provider.is_configured():
        stats.notes.append("provider not configured (missing API key)")
        log("[INFO] Exa discovery skipped: no API key configured; falling back.")
        return [], stats

    templates = build_keyword_templates(keywords)
    if not templates:
        stats.notes.append("no search keywords supplied")
        log("[ERROR] No search keywords to query.")
        return [], stats

    # Be transparent: show exactly which queries the keywords produced.
    log(
        "[INFO] Search keywords: "
        + ", ".join(k.strip() for k in keywords if k.strip())
    )
    log("[INFO] Exa queries: " + " | ".join(t.label for t in templates))

    candidates: List[str] = []

    for template in templates:
        if stats.queries_run >= max_queries:
            stats.notes.append(f"per-run query cap reached ({max_queries})")
            break
        try:
            hits = provider.search(
                template.query,
                num_results=num_results,
                include_domains=list(template.include_domains) or None,
                start_published_date=start_published_date,
            )
        except SearchProviderExhaustedError as exc:
            stats.notes.append(f"provider exhausted: {exc}")
            log(f"[WARN] Exa free allowance exhausted: {exc}")
            break
        except SearchProviderRateLimitError as exc:
            stats.notes.append(f"rate limited: {exc}")
            log(f"[WARN] Exa rate limited: {exc}")
            break
        except SearchProviderError as exc:
            stats.notes.append(f"{template.label}: {exc}")
            log(f"[WARN] Exa query failed for {template.label}: {exc}")
            continue

        stats.queries_run += 1
        stats.results_total += len(hits)
        for hit in hits:
            urls, kind = candidate_urls_from_search_hit(hit.url)
            if kind == "direct":
                candidates.extend(urls)
                stats.direct_hits += len(urls)
            elif kind == "mirror":
                candidates.extend(urls)
                stats.mirror_hits += len(urls)
            else:
                stats.non_status_discarded += 1

    stats.cost_usd = float(getattr(provider, "total_cost_usd", 0.0) or 0.0)
    log(
        f"[INFO] Exa discovery: {stats.queries_run} quer(y/ies), "
        f"{stats.direct_hits} direct + {stats.mirror_hits} mirror status link(s), "
        f"{stats.non_status_discarded} non-status discarded, "
        f"hit-rate {stats.status_hit_rate:.0%}, cost ${stats.cost_usd:.4f}."
    )
    return dedupe_candidates(candidates), stats


# ---------------------------------------------------------------------------
# Result persistence
# ---------------------------------------------------------------------------


_DATE_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")


def prune_old_results(
    output_dir: Path,
    retention_days: int,
    now: Optional[datetime] = None,
) -> List[Path]:
    """Deletes daily result files older than ``retention_days`` (UTC).

    Non-dated files (e.g. a README) are never touched. Returns deleted paths.
    """
    if retention_days <= 0:
        return []
    now = now or datetime.now(timezone.utc)
    if not output_dir.exists():
        return []

    cutoff = now.astimezone(timezone.utc).date() - timedelta(days=retention_days)
    removed: List[Path] = []
    for path in sorted(output_dir.glob("*.json")):
        match = _DATE_FILE_RE.match(path.name)
        if not match:
            continue
        try:
            file_date = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if file_date < cutoff:
            path.unlink()
            removed.append(path)
    return removed


def record_to_json_safe(record: Dict) -> Dict:
    """Serialises a record with datetimes converted to ISO 8601 strings."""
    safe = dict(record)
    for key, value in safe.items():
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            safe[key] = value.astimezone(timezone.utc).isoformat()
    return safe


def validate_record(record: Dict) -> List[str]:
    """Returns a list of schema violations (empty list == valid)."""
    required = (
        "post_url",
        "post_id",
        "post_text",
        "published_at",
        "views",
        "model_or_topic",
        "video_present",
        "video_accessible",
        "verification_status",
        "evidence_source",
        "discovered_at",
    )
    errors: List[str] = []
    for key in required:
        if key not in record:
            errors.append(f"missing field: {key}")

    if record.get("post_url") and normalize_post_url(record["post_url"]) is None:
        errors.append("post_url is not a valid X status URL")
    if record.get("post_id") and not re.fullmatch(r"\d+", str(record["post_id"])):
        errors.append("post_id must be numeric")
    if not isinstance(record.get("views"), int) or isinstance(record.get("views"), bool):
        errors.append("views must be an integer")
    for bool_field in ("video_present", "video_accessible"):
        if not isinstance(record.get(bool_field), bool):
            errors.append(f"{bool_field} must be a boolean")
    if record.get("verification_status") != "verified":
        errors.append("verification_status must be 'verified' for a qualifying record")
    for ts_field in ("published_at", "discovered_at"):
        value = record.get(ts_field)
        if not isinstance(value, str) or not _is_iso8601_aware(value):
            errors.append(f"{ts_field} must be an ISO 8601 timestamp with timezone")
    return errors


def _is_iso8601_aware(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None


def merge_records(existing: Iterable[Dict], incoming: Iterable[Dict]) -> List[Dict]:
    """Merges record lists by ``post_id``, preserving order and existing data."""
    merged: List[Dict] = []
    seen = set()
    for record in list(existing) + list(incoming):
        post_id = str(record.get("post_id") or "")
        if not post_id or post_id in seen:
            continue
        seen.add(post_id)
        merged.append(record)
    return merged


def results_path(output_dir: Path, now: datetime) -> Path:
    """Returns the dated output path ``viral-links/YYYY-MM-DD.json`` (UTC)."""
    return output_dir / f"{now.astimezone(timezone.utc).date().isoformat()}.json"


def load_existing_records(path: Path) -> List[Dict]:
    """Loads previously written records, tolerating a missing/invalid file."""
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    records = payload.get("records") if isinstance(payload, dict) else payload
    return records if isinstance(records, list) else []


def write_results(
    records: Sequence[Dict],
    output_dir: Path,
    now: datetime,
) -> Optional[Path]:
    """Merges new records into the dated file.

    Returns the written path, or ``None`` when there is nothing new to write
    (so no empty file is ever created).
    """
    if not records:
        return None

    for record in records:
        errors = validate_record(record_to_json_safe(record))
        if errors:
            raise ValueError(f"refusing to write invalid record: {errors}")

    output_dir.mkdir(parents=True, exist_ok=True)
    path = results_path(output_dir, now)
    existing = load_existing_records(path)
    merged = merge_records(existing, [record_to_json_safe(r) for r in records])

    payload = {
        "generated_at": now.astimezone(timezone.utc).isoformat(),
        "source": "find-viral-x-links",
        "records": merged,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _split_candidates(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    return [part.strip() for part in re.split(r"[\s,]+", raw) if part.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover and verify viral AI-demo videos on X (free, $0)."
    )
    parser.add_argument("--query", default="AI model demo", help="Search query for discovery.")
    parser.add_argument(
        "--candidate-urls",
        default="",
        help="Whitespace/comma-separated public X post URLs to verify.",
    )
    parser.add_argument(
        "--candidate-file",
        default="",
        help="Optional file containing candidate X post URLs (one per line).",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=DEFAULT_MAX_CANDIDATES,
        help=f"Maximum candidates to verify (default: {DEFAULT_MAX_CANDIDATES}).",
    )
    parser.add_argument(
        "--min-views",
        type=int,
        default=DEFAULT_MIN_VIEWS,
        help=f"Minimum verified views (default: {DEFAULT_MIN_VIEWS}).",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=DEFAULT_HOURS,
        help=f"Rolling time window in hours (default: {DEFAULT_HOURS}).",
    )
    parser.add_argument(
        "--output-dir", default=DEFAULT_OUTPUT_DIR, help="Output directory for JSON results."
    )
    parser.add_argument(
        "--discover-exa",
        dest="discover_exa",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Discover candidates using the free Exa search API.",
    )
    parser.add_argument(
        "--exa-api-key",
        default="",
        help="Exa API key (defaults to the EXA_API_KEY environment variable).",
    )
    parser.add_argument(
        "--search-keywords",
        default=None,
        help=(
            "Comma-separated search keywords you enter for this run (e.g. "
            "'Claude Code, Veo, AI coding agents'). Drives discovery verbatim."
        ),
    )
    parser.add_argument(
        "--topics",
        default="",
        help=(
            "Comma-separated discovery topics used only when --search-keywords "
            "is not supplied (defaults to built-in seed topics)."
        ),
    )
    parser.add_argument(
        "--per-run-query-cap",
        type=int,
        default=DEFAULT_PER_RUN_QUERY_CAP,
        help=f"Maximum Exa queries per run (default: {DEFAULT_PER_RUN_QUERY_CAP}).",
    )
    parser.add_argument(
        "--exa-num-results",
        type=int,
        default=DEFAULT_EXA_NUM_RESULTS,
        help=f"Exa results per query, 1-100 (default: {DEFAULT_EXA_NUM_RESULTS}).",
    )
    parser.add_argument(
        "--exa-since-hours",
        type=int,
        default=0,
        help="Bias Exa to content published within N hours (0 = no date filter).",
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=DEFAULT_RETENTION_DAYS,
        help=f"Prune result files older than N days (default: {DEFAULT_RETENTION_DAYS}).",
    )
    parser.add_argument(
        "--prune",
        dest="prune",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Delete result files older than --retention-days.",
    )
    parser.add_argument(
        "--discover-hn",
        dest="discover_hn",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Also run the best-effort free Hacker News discovery source.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.max_candidates < 1:
        print("[ERROR] --max-candidates must be >= 1", file=sys.stderr)
        return 2
    if args.min_views < 0:
        print("[ERROR] --min-views must be >= 0", file=sys.stderr)
        return 2
    if args.hours < 1:
        print("[ERROR] --hours must be >= 1", file=sys.stderr)
        return 2
    if args.per_run_query_cap < 1:
        print("[ERROR] --per-run-query-cap must be >= 1", file=sys.stderr)
        return 2
    if not 1 <= args.exa_num_results <= 100:
        print("[ERROR] --exa-num-results must be between 1 and 100", file=sys.stderr)
        return 2
    if args.retention_days < 0:
        print("[ERROR] --retention-days must be >= 0", file=sys.stderr)
        return 2
    if args.exa_since_hours < 0:
        print("[ERROR] --exa-since-hours must be >= 0", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)
    candidates = _split_candidates(args.candidate_urls)
    exa_stats: Optional[DiscoveryStats] = None

    if args.candidate_file:
        candidate_file = Path(args.candidate_file)
        if candidate_file.exists():
            candidates.extend(_split_candidates(candidate_file.read_text(encoding="utf-8")))
        else:
            print(f"[WARN] candidate file not found: {candidate_file}")

    if args.discover_exa:
        # Custom keywords drive discovery. When --search-keywords is supplied it
        # is authoritative; an explicitly empty/invalid value is a hard error
        # rather than a silent fallback to generic queries.
        if args.search_keywords is not None:
            keywords = parse_search_keywords(args.search_keywords)
            if not keywords:
                print(
                    "[ERROR] --search-keywords must contain at least one non-empty "
                    "comma-separated term.",
                    file=sys.stderr,
                )
                return 2
        elif args.topics:
            keywords = parse_search_keywords(args.topics) or list(DEFAULT_TOPICS)
        else:
            keywords = list(DEFAULT_TOPICS)

        api_key = args.exa_api_key or os.getenv("EXA_API_KEY", "")
        provider = ExaSearchProvider(api_key)
        start_date = None
        if args.exa_since_hours > 0:
            start_date = (now - timedelta(hours=args.exa_since_hours)).replace(
                microsecond=0
            ).isoformat()
        exa_candidates, exa_stats = discover_with_exa(
            provider,
            keywords,
            max_queries=args.per_run_query_cap,
            num_results=args.exa_num_results,
            start_published_date=start_date,
        )
        candidates.extend(exa_candidates)

    if args.discover_hn:
        print(f"[INFO] Running best-effort Hacker News discovery for query={args.query!r} ...")
        discovered = discover_from_hackernews(args.query, hours=args.hours, now=now)
        print(f"[INFO] Hacker News discovery found {len(discovered)} candidate link(s).")
        candidates.extend(discovered)

    candidates = dedupe_candidates(candidates)
    print(f"[INFO] {len(candidates)} unique candidate(s) to verify (cap {args.max_candidates}).")

    if not candidates:
        print(
            "[RESULT] No candidates to verify. Provide --candidate-urls, or a candidate "
            "file; automated search-engine discovery is not available for $0 "
            "(see docs/x-browser-discovery.md)."
        )
        return 0

    try:
        records, rejections = verify_candidates(
            candidates,
            min_views=args.min_views,
            hours=args.hours,
            now=now,
            max_candidates=args.max_candidates,
        )
    except CapabilityUnavailableError as exc:
        print(
            f"[ERROR] Required capability unavailable: cannot read public X metadata "
            f"from this environment ({exc}).",
            file=sys.stderr,
        )
        return 3

    output_dir = Path(args.output_dir)
    written = write_results(records, output_dir, now)

    pruned: List[Path] = []
    if args.prune:
        pruned = prune_old_results(output_dir, args.retention_days, now)

    print("\n=== Summary ===")
    if args.discover_exa and args.search_keywords is not None:
        print(
            "Search keywords            : "
            + ", ".join(parse_search_keywords(args.search_keywords))
        )
    if exa_stats is not None:
        print(
            f"Exa discovery               : {exa_stats.queries_run} queries, "
            f"{exa_stats.direct_hits} direct + {exa_stats.mirror_hits} mirror status links, "
            f"{exa_stats.non_status_discarded} non-status discarded, "
            f"cost ${exa_stats.cost_usd:.4f}"
        )
        for note in exa_stats.notes:
            print(f"  - {note}")
    print(f"Verified qualifying records : {len(records)}")
    print(f"Excluded candidates         : {len(rejections)}")
    if args.prune:
        print(f"Pruned old result files     : {len(pruned)} (retention {args.retention_days}d)")
    if written:
        print(f"Results file                : {written}")
    else:
        print(
            "Results file                : none (no qualifying records; no file written)"
        )

    if not records:
        print(
            "[RESULT] No qualifying posts were verified. This is a genuine empty "
            "result, not a fabricated success."
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

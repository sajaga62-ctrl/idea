"""Free ($0) viral AI-demo link finder for X (Twitter).

Discovers *candidate* public X post URLs (manual ingestion plus a best-effort
free aggregator) and then **verifies** each candidate with unauthenticated,
permitted metadata: yt-dlp's public X/Twitter extractor and a ranged HTTP
check against the video CDN.

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
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import yt_dlp

from src.downloader import extract_post_id, validate_x_url

DEFAULT_MIN_VIEWS = 50_000
DEFAULT_HOURS = 24
DEFAULT_MAX_CANDIDATES = 25
DEFAULT_OUTPUT_DIR = "viral-links"

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
# Result persistence
# ---------------------------------------------------------------------------


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

    now = datetime.now(timezone.utc)
    candidates = _split_candidates(args.candidate_urls)

    if args.candidate_file:
        candidate_file = Path(args.candidate_file)
        if candidate_file.exists():
            candidates.extend(_split_candidates(candidate_file.read_text(encoding="utf-8")))
        else:
            print(f"[WARN] candidate file not found: {candidate_file}")

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

    print("\n=== Summary ===")
    print(f"Verified qualifying records : {len(records)}")
    print(f"Excluded candidates         : {len(rejections)}")
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

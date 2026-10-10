"""Free-tier web search provider (Exa) for X-post discovery.

Exa's `/search` endpoint is a plain HTTPS API that needs only an ``x-api-key``
header, so it works from GitHub-hosted CI where browser search and direct
``x.com`` page access are Cloudflare/JS blocked. The free tier requires no
payment method and its credits refresh monthly.

This module defines:

* :class:`SearchProvider` — a tiny protocol so Exa is the default but any other
  provider could be swapped in without touching the pipeline.
* :class:`ExaSearchProvider` — an ``urllib``-based client (no extra dependency).
* Query templates derived from seed topics, mixing semantic phrases, a domain
  filter, and the literal ``site:x.com`` operator.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Protocol, Sequence, Tuple

EXA_SEARCH_URL = "https://api.exa.ai/search"
DEFAULT_EXA_SEARCH_TYPE = "auto"
DEFAULT_EXA_NUM_RESULTS = 10
# Per-run number of Exa queries. The monthly free allowance is generous (thousands
# of searches); this cap keeps a single run from silently consuming the whole
# month. It is a default, overridable per run.
DEFAULT_PER_RUN_QUERY_CAP = 25
X_DOMAINS: Tuple[str, ...] = ("x.com", "twitter.com")
# Tokens that reliably surface aggregator/mirror pages embedding X post ids,
# because Exa does not index x.com status pages directly.
SOCIAL_HINT = "twitter x post video"

# Seed topics: frontier-model releases, computer-use/browser agents, and
# "anything made by a frontier model".
DEFAULT_TOPICS: Tuple[str, ...] = (
    "new frontier AI model release demo video",
    "AI coding agent demo video",
    "computer use browser agent demo video",
    "AI video generation model demo",
    "multimodal AI model demonstration",
    "humanoid robot AI demonstration",
)


class SearchProviderError(Exception):
    """Base error for search-provider failures."""


class SearchProviderAuthError(SearchProviderError):
    """Raised when the provider rejects or is missing credentials."""


class SearchProviderRateLimitError(SearchProviderError):
    """Raised when the provider keeps returning HTTP 429 after retries."""


class SearchProviderExhaustedError(SearchProviderError):
    """Raised when the provider reports the free allowance/credits are used up."""


@dataclass(frozen=True)
class SearchHit:
    """A single search result: a URL plus whatever context the provider gives."""

    url: str
    title: str = ""
    published_date: Optional[str] = None
    provider: str = "exa"


class SearchProvider(Protocol):
    """Minimal interface the discovery pipeline depends on."""

    name: str

    def is_configured(self) -> bool:  # pragma: no cover - protocol
        ...

    def search(
        self,
        query: str,
        *,
        num_results: int = DEFAULT_EXA_NUM_RESULTS,
        include_domains: Optional[Sequence[str]] = None,
        start_published_date: Optional[str] = None,
    ) -> List[SearchHit]:  # pragma: no cover - protocol
        ...


def _safe_error_body(exc: urllib.error.HTTPError) -> str:
    """Reads a short, non-sensitive snippet of an error response body."""
    try:
        raw = exc.read()
    except Exception:  # noqa: BLE001 - best effort only
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")[:200]
    return str(raw)[:200]


class ExaSearchProvider:
    """Exa ``/search`` client over ``urllib`` with bounded backoff."""

    name = "exa"

    def __init__(
        self,
        api_key: Optional[str],
        *,
        search_type: str = DEFAULT_EXA_SEARCH_TYPE,
        timeout: int = 30,
        max_retries: int = 3,
        backoff_seconds: float = 2.0,
        opener: Callable[..., object] = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_key = (api_key or "").strip()
        self.search_type = search_type
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.backoff_seconds = max(0.0, float(backoff_seconds))
        self._opener = opener
        self._sleep = sleep
        self.queries_used = 0
        self.total_cost_usd = 0.0

    def is_configured(self) -> bool:
        """True when an API key is present."""
        return bool(self._api_key)

    def search(
        self,
        query: str,
        *,
        num_results: int = DEFAULT_EXA_NUM_RESULTS,
        include_domains: Optional[Sequence[str]] = None,
        start_published_date: Optional[str] = None,
    ) -> List[SearchHit]:
        """Runs one Exa search and returns normalised hits.

        Raises:
            SearchProviderAuthError: missing/invalid key (HTTP 401/403).
            SearchProviderExhaustedError: free allowance used up (HTTP 402).
            SearchProviderRateLimitError: still rate limited after retries (429).
            SearchProviderError: any other request/parse failure.
        """
        if not self.is_configured():
            raise SearchProviderAuthError("EXA_API_KEY is not set")

        count = max(1, min(int(num_results), 100))
        payload: dict = {
            "query": query,
            "type": self.search_type,
            "numResults": count,
        }
        if include_domains:
            payload["includeDomains"] = list(include_domains)
        if start_published_date:
            payload["startPublishedDate"] = start_published_date

        request = urllib.request.Request(
            EXA_SEARCH_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "x-api-key": self._api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "viral-x-finder/1.0 (+free-tier)",
            },
            method="POST",
        )

        body: Optional[str] = None
        attempt = 0
        while attempt <= self.max_retries:
            try:
                with self._opener(request, timeout=self.timeout) as response:
                    body = response.read().decode("utf-8", "replace")
                break
            except urllib.error.HTTPError as exc:
                status = exc.code
                detail = _safe_error_body(exc)
                if status in (401, 403):
                    raise SearchProviderAuthError(
                        f"Exa rejected the API key (HTTP {status}): {detail}"
                    ) from exc
                if status == 402:
                    raise SearchProviderExhaustedError(
                        f"Exa free allowance exhausted (HTTP {status}): {detail}"
                    ) from exc
                if status == 429:
                    if attempt < self.max_retries:
                        self._sleep(self.backoff_seconds * (2**attempt))
                        attempt += 1
                        continue
                    raise SearchProviderRateLimitError(
                        "Exa rate limited the request (HTTP 429) after retries"
                    ) from exc
                if 500 <= status < 600 and attempt < self.max_retries:
                    self._sleep(self.backoff_seconds * (2**attempt))
                    attempt += 1
                    continue
                raise SearchProviderError(f"Exa request failed (HTTP {status}): {detail}") from exc
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                if attempt < self.max_retries:
                    self._sleep(self.backoff_seconds * (2**attempt))
                    attempt += 1
                    continue
                raise SearchProviderError(f"Exa request failed: {exc}") from exc

        if body is None:  # pragma: no cover - defensive
            raise SearchProviderError("Exa request produced no response")

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise SearchProviderError(f"Exa returned invalid JSON: {exc}") from exc

        self.queries_used += 1
        cost = parsed.get("costDollars")
        if isinstance(cost, dict) and isinstance(cost.get("total"), (int, float)):
            self.total_cost_usd += float(cost["total"])

        hits: List[SearchHit] = []
        for item in parsed.get("results") or []:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not url:
                continue
            hits.append(
                SearchHit(
                    url=str(url),
                    title=str(item.get("title") or ""),
                    published_date=item.get("publishedDate"),
                    provider=self.name,
                )
            )
        return hits


@dataclass(frozen=True)
class QueryTemplate:
    """A single discovery query plus the domain filter it should use."""

    label: str
    query: str
    include_domains: Tuple[str, ...] = ()


def build_query_templates(
    topics: Sequence[str] = DEFAULT_TOPICS,
    *,
    site_operator: bool = True,
    social_hint: bool = True,
) -> List[QueryTemplate]:
    """Builds a mix of semantic, domain-filtered, ``site:`` and social templates.

    The ``social`` variant (topic + "twitter x post video") is the highest-yield
    in practice: Exa cannot index `x.com` status pages, but aggregator/mirror
    pages that embed the post id are indexed and surface through these tokens.
    """
    templates: List[QueryTemplate] = []
    for topic in topics:
        clean = topic.strip()
        if not clean:
            continue
        templates.append(QueryTemplate(label=f"semantic:{clean}", query=clean))
        templates.append(
            QueryTemplate(
                label=f"domain:{clean}",
                query=clean,
                include_domains=X_DOMAINS,
            )
        )
        if site_operator:
            templates.append(
                QueryTemplate(label=f"site:{clean}", query=f"site:x.com {clean}")
            )
        if social_hint:
            templates.append(
                QueryTemplate(label=f"social:{clean}", query=f"{clean} {SOCIAL_HINT}")
            )
    return templates

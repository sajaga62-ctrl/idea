"""Tests for the Exa search provider.

Unit tests inject a fake HTTP opener so they are deterministic and offline. The
real integration test is gated on ``EXA_API_KEY`` and reports **blocked** when
the key is absent — it is never counted as passing without running.
"""

import io
import json
import os
import urllib.error

import pytest

from src.exa_search import (
    DEFAULT_TOPICS,
    ExaSearchProvider,
    SearchProviderAuthError,
    SearchProviderError,
    SearchProviderExhaustedError,
    SearchProviderRateLimitError,
    X_DOMAINS,
    build_query_templates,
)


class FakeResponse:
    def __init__(self, payload, status=200):
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status
        self.headers = {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, body=b'{"error":"boom"}'):
    return urllib.error.HTTPError(
        "https://api.exa.ai/search", code, "err", {}, io.BytesIO(body)
    )


class RecordingOpener:
    """Opener that records requests and replays a queue of responses/errors."""

    def __init__(self, *events):
        self.events = list(events)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        event = self.events.pop(0) if self.events else FakeResponse({"results": []})
        if isinstance(event, Exception):
            raise event
        return event

    @property
    def last_body(self):
        return json.loads(self.requests[-1].data.decode("utf-8"))


def make_provider(opener, **kwargs):
    kwargs.setdefault("sleep", lambda _s: None)
    return ExaSearchProvider("test-key", opener=opener, **kwargs)


class TestConfiguration:
    def test_missing_key_is_not_configured(self):
        assert ExaSearchProvider("").is_configured() is False
        assert ExaSearchProvider(None).is_configured() is False

    def test_present_key_is_configured(self):
        assert ExaSearchProvider("abc").is_configured() is True

    def test_search_without_key_raises_auth_error(self):
        provider = ExaSearchProvider("")
        with pytest.raises(SearchProviderAuthError):
            provider.search("q")


class TestSearchParsing:
    def test_parses_results_and_cost(self):
        payload = {
            "results": [
                {"url": "https://x.com/a/status/123", "title": "A", "publishedDate": "2026-01-01T00:00:00Z"},
                {"url": "https://mirror.example/v/456", "title": "B"},
                {"title": "no url"},
            ],
            "costDollars": {"total": 0.005},
        }
        opener = RecordingOpener(FakeResponse(payload))
        provider = make_provider(opener)

        hits = provider.search("query", num_results=7, include_domains=X_DOMAINS)

        assert [h.url for h in hits] == [
            "https://x.com/a/status/123",
            "https://mirror.example/v/456",
        ]
        assert hits[0].title == "A"
        assert hits[0].published_date == "2026-01-01T00:00:00Z"
        assert hits[0].provider == "exa"
        assert provider.queries_used == 1
        assert provider.total_cost_usd == pytest.approx(0.005)

    def test_request_body_contains_filters(self):
        opener = RecordingOpener(FakeResponse({"results": []}))
        provider = make_provider(opener)
        provider.search(
            "hello",
            num_results=3,
            include_domains=["x.com"],
            start_published_date="2026-10-01T00:00:00Z",
        )
        body = opener.last_body
        assert body["query"] == "hello"
        assert body["numResults"] == 3
        assert body["includeDomains"] == ["x.com"]
        assert body["startPublishedDate"] == "2026-10-01T00:00:00Z"
        assert opener.requests[-1].headers.get("X-api-key") == "test-key"

    def test_num_results_is_clamped(self):
        opener = RecordingOpener(FakeResponse({"results": []}))
        provider = make_provider(opener)
        provider.search("q", num_results=9999)
        assert opener.last_body["numResults"] == 100
        provider.search("q", num_results=0)
        assert opener.last_body["numResults"] == 1


class TestErrorHandling:
    def test_401_raises_auth_error(self):
        provider = make_provider(RecordingOpener(http_error(401)))
        with pytest.raises(SearchProviderAuthError):
            provider.search("q")

    def test_403_raises_auth_error(self):
        provider = make_provider(RecordingOpener(http_error(403)))
        with pytest.raises(SearchProviderAuthError):
            provider.search("q")

    def test_402_raises_exhausted(self):
        provider = make_provider(RecordingOpener(http_error(402)))
        with pytest.raises(SearchProviderExhaustedError):
            provider.search("q")

    def test_429_retries_then_raises_rate_limit(self):
        opener = RecordingOpener(http_error(429), http_error(429))
        provider = make_provider(opener, max_retries=1)
        with pytest.raises(SearchProviderRateLimitError):
            provider.search("q")
        assert len(opener.requests) == 2

    def test_server_error_is_retried_then_succeeds(self):
        opener = RecordingOpener(http_error(500), FakeResponse({"results": [{"url": "u"}]}))
        provider = make_provider(opener, max_retries=2)
        hits = provider.search("q")
        assert [h.url for h in hits] == ["u"]
        assert len(opener.requests) == 2

    def test_network_error_retried_then_raises(self):
        opener = RecordingOpener(urllib.error.URLError("down"), urllib.error.URLError("down"))
        provider = make_provider(opener, max_retries=1)
        with pytest.raises(SearchProviderError):
            provider.search("q")

    def test_invalid_json_raises_provider_error(self):
        opener = RecordingOpener(FakeResponse.__new__(FakeResponse))
        opener.events = [_RawResponse(b"not json")]
        provider = make_provider(opener)
        with pytest.raises(SearchProviderError):
            provider.search("q")


class _RawResponse(FakeResponse):
    def __init__(self, raw):
        self._body = raw
        self.status = 200
        self.headers = {}


class TestQueryTemplates:
    def test_default_topics_non_empty(self):
        assert len(DEFAULT_TOPICS) >= 4

    def test_templates_are_a_mix(self):
        templates = build_query_templates(["ai agent demo"])
        labels = [t.label for t in templates]
        assert any(l.startswith("semantic:") for l in labels)
        assert any(l.startswith("domain:") for l in labels)
        assert any(l.startswith("site:") for l in labels)
        assert any(l.startswith("social:") for l in labels)

    def test_domain_and_social_templates_are_well_formed(self):
        templates = build_query_templates(["ai agent demo"])
        domain = next(t for t in templates if t.label.startswith("domain:"))
        social = next(t for t in templates if t.label.startswith("social:"))
        assert domain.include_domains == X_DOMAINS
        assert "twitter" in social.query and "x" in social.query

    def test_blank_topics_are_skipped(self):
        assert build_query_templates(["", "  "]) == []


# ---------------------------------------------------------------------------
# REAL integration test (network + key; reports blocked instead of passing)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_integration_exa_search_real():
    """Proves the real Exa path works when a key is available."""
    api_key = os.getenv("EXA_API_KEY")
    if not api_key:
        pytest.skip("BLOCKED: EXA_API_KEY not set; real Exa path not exercised")

    provider = ExaSearchProvider(api_key)
    try:
        hits = provider.search("computer use agent demo video", num_results=5)
    except SearchProviderAuthError as exc:
        pytest.fail(f"EXA_API_KEY is set but rejected: {exc}")
    except SearchProviderError as exc:
        pytest.skip(f"BLOCKED: Exa unreachable from this environment ({exc})")

    assert isinstance(hits, list)
    assert provider.queries_used == 1

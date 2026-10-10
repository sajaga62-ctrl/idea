"""Unit and integration tests for the free viral AI-demo link finder.

Unit tests use injected/mocked metadata so they are deterministic and offline.
Integration tests hit real public resources and are explicitly marked; if the
network blocks them they report ``blocked`` (skipped) instead of passing.
"""

import json
import socket
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from src import viral_finder as vf
from src.downloader import extract_post_id
from src.exa_search import (
    SearchHit,
    SearchProviderError,
    SearchProviderExhaustedError,
)


NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def synth_info(url: str = "https://video.twimg.com/x.mp4", duration: float = 12.0):
    """Minimal yt-dlp-shaped payload with one real video format."""
    return {
        "id": "111",
        "formats": [{"format_id": "http-1", "url": url, "ext": "mp4", "height": 720}],
        "duration": duration,
        "title": "AI agent demo",
    }


def synth_metadata(
    *,
    post_id: str = "111222333",
    text: str = "Watch our new AI agent code an app end to end",
    published_at: datetime = None,
    views: int = 100_000,
    info=None,
):
    return {
        "post_id": post_id,
        "post_text": text,
        "published_at": published_at or NOW - timedelta(hours=1),
        "views": views,
        "info": info if info is not None else synth_info(),
    }


# ---------------------------------------------------------------------------
# URL normalisation + post id extraction
# ---------------------------------------------------------------------------


class TestURLNormalization:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            (
                "https://x.com/OpenAI/status/1790637656616943991",
                "https://x.com/OpenAI/status/1790637656616943991",
            ),
            (
                "https://twitter.com/OpenAI/status/1790637656616943991?s=20&t=ab",
                "https://x.com/OpenAI/status/1790637656616943991",
            ),
            ("https://www.x.com/user/status/123456789", "https://x.com/user/status/123456789"),
            (
                "https://mobile.twitter.com/user/status/123/video/1",
                "https://x.com/user/status/123",
            ),
            ("https://x.com/user/statuses/555", "https://x.com/user/status/555"),
            (
                "https://x.com/i/web/status/1790637656616943991",
                "https://x.com/i/web/status/1790637656616943991",
            ),
            # embedded inside surrounding text
            (
                "see https://x.com/foo/status/42 here",
                "https://x.com/foo/status/42",
            ),
            # HTML-escaped query string
            (
                "https://x.com/foo/status/42?a=1&amp;b=2",
                "https://x.com/foo/status/42",
            ),
        ],
    )
    def test_normalizes_valid_references(self, raw, expected):
        assert vf.normalize_post_url(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            None,
            "not a url",
            "https://youtube.com/watch?v=123",
            "https://x.com/OpenAI",
            "https://x.com/search?q=ai",
            "https://x.com/user/status/not_numeric",
            "https://reddit.com/r/x/comments/1",
        ],
    )
    def test_rejects_invalid_references(self, raw):
        assert vf.normalize_post_url(raw) is None

    def test_extract_candidate_urls_from_text(self):
        text = (
            "check https://t.co/abc and https://x.com/a/status/111 and "
            "https://twitter.com/b/status/222?s=1 plus https://x.com/a/status/111"
        )
        assert vf.extract_candidate_urls(text) == [
            "https://x.com/a/status/111",
            "https://x.com/b/status/222",
        ]

    def test_post_id_extraction_matches_downloader(self):
        url = "https://x.com/OpenAI/status/1790637656616943991"
        assert vf.normalize_post_url(url) == url
        assert extract_post_id(url) == "1790637656616943991"


# ---------------------------------------------------------------------------
# De-duplication
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_dedupe_candidates_by_post_id(self):
        urls = [
            "https://x.com/a/status/111",
            "https://twitter.com/b/status/111",  # same id, different handle
            "https://x.com/c/status/333",
            "https://youtube.com/watch?v=1",  # invalid
        ]
        assert vf.dedupe_candidates(urls) == [
            "https://x.com/a/status/111",
            "https://x.com/c/status/333",
        ]

    def test_merge_records_preserves_order_and_dedupes(self):
        existing = [{"post_id": "1", "post_url": "u1"}]
        incoming = [
            {"post_id": "2", "post_url": "u2"},
            {"post_id": "1", "post_url": "u1-dup"},
        ]
        merged = vf.merge_records(existing, incoming)
        assert [r["post_id"] for r in merged] == ["1", "2"]
        assert merged[0]["post_url"] == "u1"

    def test_verify_candidates_dedupes_before_verifying(self):
        calls = []

        def fetcher(url):
            calls.append(url)
            return synth_metadata(post_id="111")

        records, _ = vf.verify_candidates(
            ["https://x.com/a/status/111", "https://twitter.com/b/status/111"],
            now=NOW,
            metadata_fetcher=fetcher,
            accessibility_checker=lambda info: True,
        )
        assert len(calls) == 1
        assert len(records) == 1


# ---------------------------------------------------------------------------
# Rolling time window
# ---------------------------------------------------------------------------


class TestRollingWindow:
    def test_inside_window(self):
        assert vf.within_window(NOW - timedelta(hours=23), NOW, 24) is True

    def test_outside_window(self):
        assert vf.within_window(NOW - timedelta(hours=25), NOW, 24) is False

    def test_boundary_is_inclusive(self):
        assert vf.within_window(NOW - timedelta(hours=24), NOW, 24) is True

    def test_configurable_window(self):
        published = NOW - timedelta(hours=48)
        assert vf.within_window(published, NOW, 24) is False
        assert vf.within_window(published, NOW, 72) is True

    def test_future_and_naive_timestamps_rejected(self):
        assert vf.within_window(NOW + timedelta(hours=2), NOW, 24) is False
        assert vf.within_window(datetime(2026, 10, 10, 11, 0), NOW, 24) is False
        assert vf.within_window(None, NOW, 24) is False

    def test_parse_published_at(self):
        assert vf.parse_published_at(1715756260) == datetime(
            2024, 5, 15, 6, 57, 40, tzinfo=timezone.utc
        )
        assert vf.parse_published_at(None) is None
        assert vf.parse_published_at("nope") is None


# ---------------------------------------------------------------------------
# View threshold
# ---------------------------------------------------------------------------


class TestViewThreshold:
    def test_default_50k_boundary(self):
        assert vf.meets_view_threshold(50_000, vf.DEFAULT_MIN_VIEWS) is True
        assert vf.DEFAULT_MIN_VIEWS == 50_000
        assert vf.meets_view_threshold(49_999, vf.DEFAULT_MIN_VIEWS) is False

    def test_configurable_threshold(self):
        assert vf.meets_view_threshold(100, 100) is True
        assert vf.meets_view_threshold(99, 100) is False

    @pytest.mark.parametrize("bad", [None, True, False, "abc", "12345x"])
    def test_unverifiable_view_counts_rejected(self, bad):
        assert vf.meets_view_threshold(bad, 0) is False

    def test_numeric_string_accepted(self):
        assert vf.meets_view_threshold("75000", 50_000) is True


# ---------------------------------------------------------------------------
# AI relevance
# ---------------------------------------------------------------------------


class TestAIRelevance:
    @pytest.mark.parametrize(
        "text",
        [
            "New GPT-5 demo is unreal",
            "Our AI agent built a whole app",
            "Computer-use agent browsing the web",
            "Introducing our text-to-video model",
            "Humanoid robot learns to walk",
            "Gemini multimodal demo",
        ],
    )
    def test_relevant(self, text):
        assert vf.is_ai_related(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "email me the said html file",
            "This is a food review",
            "Python 3.14 released",
            None,
        ],
    )
    def test_irrelevant(self, text):
        assert vf.is_ai_related(text) is False

    def test_detect_topic_specific_and_generic(self):
        assert vf.detect_topic("Sora text-to-video generation") == "OpenAI Sora"
        assert vf.detect_topic("New Claude agent") == "Anthropic Claude"
        assert vf.detect_topic("some ai thing") == "AI (unspecified)"


# ---------------------------------------------------------------------------
# Video presence + verification status
# ---------------------------------------------------------------------------


class TestVideoPresenceAndStatus:
    def test_video_present_detection(self):
        assert vf.video_is_present(synth_info()) is True
        assert vf.video_is_present({"formats": [], "duration": 5}) is False
        assert vf.video_is_present({"formats": [{"url": "u"}], "duration": 0}) is False

    def test_qualifying_record_is_verified(self):
        record, reason = vf.build_record(
            "https://x.com/OpenAI/status/111222333",
            synth_metadata(),
            min_views=50_000,
            hours=24,
            now=NOW,
            video_accessible=True,
        )
        assert reason == "verified"
        assert record["verification_status"] == "verified"
        assert record["video_present"] is True
        assert record["video_accessible"] is True
        assert validate_ok(record)

    def test_records_include_required_schema(self):
        record, _ = vf.build_record(
            "https://x.com/OpenAI/status/111222333",
            synth_metadata(),
            min_views=50_000,
            hours=24,
            now=NOW,
            video_accessible=True,
        )
        assert set(vf.validate_record(record)) == set()


def validate_ok(record):
    return vf.validate_record(record) == []


# ---------------------------------------------------------------------------
# Missing / invalid metadata -> excluded
# ---------------------------------------------------------------------------


class TestMissingMetadata:
    def test_missing_video_excluded(self):
        meta = synth_metadata(info={"formats": [], "duration": 0})
        record, reason = vf.build_record(
            "https://x.com/u/status/111", meta, min_views=0, hours=24, now=NOW,
            video_accessible=True,
        )
        assert record is None
        assert "video" in reason

    def test_missing_timestamp_excluded(self):
        meta = synth_metadata(published_at=None)
        meta["published_at"] = None
        record, reason = vf.build_record(
            "https://x.com/u/status/111", meta, min_views=0, hours=24, now=NOW,
            video_accessible=True,
        )
        assert record is None
        assert "timestamp" in reason

    def test_missing_views_excluded(self):
        meta = synth_metadata(views=None)
        record, reason = vf.build_record(
            "https://x.com/u/status/111", meta, min_views=50_000, hours=24, now=NOW,
            video_accessible=True,
        )
        assert record is None
        assert "views" in reason

    def test_irrelevant_post_excluded(self):
        meta = synth_metadata(text="Best pizza in town, watch the cheese pull")
        record, reason = vf.build_record(
            "https://x.com/u/status/111", meta, min_views=0, hours=24, now=NOW,
            video_accessible=True,
        )
        assert record is None
        assert "AI" in reason

    def test_inaccessible_video_excluded(self):
        record, reason = vf.build_record(
            "https://x.com/u/status/111", synth_metadata(),
            min_views=0, hours=24, now=NOW, video_accessible=False,
        )
        assert record is None
        assert "accessible" in reason

    def test_verify_candidate_handles_metadata_error(self):
        def bad_fetcher(url):
            raise vf.MetadataError("No video could be found in this tweet")

        record, reason = vf.verify_candidate(
            "https://x.com/u/status/111",
            min_views=0, hours=24, now=NOW, metadata_fetcher=bad_fetcher,
        )
        assert record is None
        assert "metadata unavailable" in reason

    def test_verify_candidate_rejects_bad_url(self):
        record, reason = vf.verify_candidate(
            "https://youtube.com/watch?v=1",
            min_views=0, hours=24, now=NOW,
        )
        assert record is None
        assert "URL" in reason


# ---------------------------------------------------------------------------
# JSON schema + timestamps
# ---------------------------------------------------------------------------


class TestJSONSchema:
    def _record(self):
        record, _ = vf.build_record(
            "https://x.com/OpenAI/status/111222333",
            synth_metadata(),
            min_views=0, hours=24, now=NOW, video_accessible=True,
        )
        return record

    def test_written_file_is_valid_json_with_timestamps(self, tmp_path):
        path = vf.write_results([self._record()], tmp_path, NOW)
        assert path == tmp_path / "2026-10-10.json"
        payload = json.loads(path.read_text())

        assert vf._is_iso8601_aware(payload["generated_at"])
        assert len(payload["records"]) == 1
        record = payload["records"][0]
        assert vf.validate_record(record) == []
        assert vf._is_iso8601_aware(record["published_at"])
        assert vf._is_iso8601_aware(record["discovered_at"])
        assert isinstance(record["views"], int)

    def test_validate_record_detects_violations(self):
        broken = {
            "post_url": "https://youtube.com/watch?v=1",
            "post_id": "abc",
            "views": "many",
            "video_present": "yes",
            "video_accessible": True,
            "verification_status": "maybe",
            "published_at": "not-a-timestamp",
            "discovered_at": "2026-10-10T12:00:00",
        }
        errors = vf.validate_record(broken)
        assert any("post_url" in e for e in errors)
        assert any("post_id" in e for e in errors)
        assert any("views" in e for e in errors)
        assert any("video_present" in e for e in errors)
        assert any("verification_status" in e for e in errors)
        assert any("published_at" in e for e in errors)
        assert any("discovered_at" in e for e in errors)

    def test_datetime_serialisation_is_aware(self):
        aware = vf.record_to_json_safe({"t": datetime(2026, 10, 10, 12, 0)})
        assert vf._is_iso8601_aware(aware["t"])

    def test_repeated_run_dedupes_on_disk(self, tmp_path):
        record = self._record()
        vf.write_results([record], tmp_path, NOW)
        again = dict(record)
        again["discovered_at"] = (NOW + timedelta(minutes=5)).isoformat()
        vf.write_results([again], tmp_path, NOW)
        payload = json.loads((tmp_path / "2026-10-10.json").read_text())
        assert len(payload["records"]) == 1


# ---------------------------------------------------------------------------
# Zero qualifying results
# ---------------------------------------------------------------------------


class TestZeroResults:
    def test_no_file_written_for_empty_results(self, tmp_path):
        assert vf.write_results([], tmp_path, NOW) is None
        assert list(tmp_path.iterdir()) == []

    def test_main_returns_zero_and_creates_no_file_without_candidates(self, tmp_path, capsys):
        code = vf.main(["--output-dir", str(tmp_path)])
        assert code == 0
        assert list(tmp_path.iterdir()) == []
        assert "No candidates to verify" in capsys.readouterr().out

    def test_invalid_cli_inputs_are_rejected(self):
        assert vf.main(["--max-candidates", "0"]) == 2
        assert vf.main(["--hours", "0"]) == 2
        assert vf.main(["--min-views", "-5"]) == 2


# ---------------------------------------------------------------------------
# Existing downloader regression
# ---------------------------------------------------------------------------


class TestDownloaderRegression:
    def test_downloader_still_parses_and_validates(self):
        from src.downloader import InvalidURLError, extract_post_id, validate_x_url

        assert extract_post_id("https://x.com/OpenAI/status/1790637656616943991") == (
            "1790637656616943991"
        )
        assert validate_x_url("https://twitter.com/u/status/42") == "42"
        with pytest.raises(InvalidURLError):
            validate_x_url("https://example.com/status/1")


# ---------------------------------------------------------------------------
# Mirror/aggregator extraction (how Exa hits become candidate X posts)
# ---------------------------------------------------------------------------


class TestMirrorExtraction:
    @pytest.mark.parametrize(
        "url, expected",
        [
            ("https://nitter.cf/spira_lab_ai/status/2107509453889401136", ["2107509453889401136"]),
            ("https://www.24vids.com/video/x-abc-tw-2105704169009246248", ["2105704169009246248"]),
            ("https://twirank.stream/video/2105476752449679686", ["2105476752449679686"]),
            ("https://example.com/post/2016925155277361423", ["2016925155277361423"]),
            ("https://x.com/OpenAI/profile", []),
            ("https://example.com/status/123", []),  # too short to be a snowflake id
            ("", []),
            (None, []),
        ],
    )
    def test_extract_mirror_status_ids(self, url, expected):
        assert vf.extract_mirror_status_ids(url) == expected

    def test_direct_url_wins(self):
        urls, kind = vf.candidate_urls_from_search_hit(
            "https://x.com/OpenAI/status/1790637656616943991"
        )
        assert kind == "direct"
        assert urls == ["https://x.com/OpenAI/status/1790637656616943991"]

    def test_mirror_url_yields_canonical_post_url(self):
        urls, kind = vf.candidate_urls_from_search_hit(
            "https://nitter.cf/spira_lab_ai/status/2107509453889401136"
        )
        assert kind == "mirror"
        assert urls == ["https://x.com/i/web/status/2107509453889401136"]

    def test_non_status_hit_is_none(self):
        urls, kind = vf.candidate_urls_from_search_hit("https://x.com/OpenAI")
        assert urls == []
        assert kind == "none"


# ---------------------------------------------------------------------------
# Exa discovery pipeline
# ---------------------------------------------------------------------------


class FakeProvider:
    name = "fake"

    def __init__(self, hits=None, *, configured=True, error=None):
        self.hits = hits or []
        self.configured = configured
        self.error = error
        self.calls = []
        self.total_cost_usd = 0.25

    def is_configured(self):
        return self.configured

    def search(self, query, **kwargs):
        self.calls.append(query)
        if self.error:
            raise self.error
        return list(self.hits)


class TestDiscoverWithExa:
    def test_extracts_direct_and_mirror_and_discards_rest(self):
        provider = FakeProvider(
            hits=[
                SearchHit("https://x.com/a/status/1111111111111111111"),
                SearchHit("https://nitter.cf/u/status/2222222222222222222"),
                SearchHit("https://x.com/someprofile"),
            ]
        )
        candidates, stats = vf.discover_with_exa(
            provider, ["ai demo"], max_queries=1, log=lambda _m: None
        )

        assert candidates == [
            "https://x.com/a/status/1111111111111111111",
            "https://x.com/i/web/status/2222222222222222222",
        ]
        assert stats.queries_run == 1
        assert stats.direct_hits == 1
        assert stats.mirror_hits == 1
        assert stats.non_status_discarded == 1
        assert stats.status_hits == 2
        assert stats.cost_usd == 0.25

    def test_respects_query_cap(self):
        provider = FakeProvider(hits=[])
        _candidates, stats = vf.discover_with_exa(
            provider, ["ai demo"], max_queries=1, log=lambda _m: None
        )
        assert stats.queries_run == 1
        assert len(provider.calls) == 1
        assert any("cap" in note for note in stats.notes)

    def test_exhaustion_stops_gracefully(self):
        provider = FakeProvider(error=SearchProviderExhaustedError("credits used"))
        candidates, stats = vf.discover_with_exa(provider, ["ai demo"], log=lambda _m: None)
        assert candidates == []
        assert len(provider.calls) == 1
        assert any("exhausted" in note for note in stats.notes)

    def test_other_errors_are_noted_and_skipped(self):
        provider = FakeProvider(error=SearchProviderError("boom"))
        candidates, stats = vf.discover_with_exa(provider, ["ai demo"], log=lambda _m: None)
        assert candidates == []
        assert stats.queries_run == 0
        assert len(stats.notes) >= 1

    def test_unconfigured_provider_returns_nothing(self):
        provider = FakeProvider(configured=False)
        candidates, stats = vf.discover_with_exa(provider, ["ai demo"], log=lambda _m: None)
        assert candidates == []
        assert provider.calls == []
        assert any("not configured" in note for note in stats.notes)


# ---------------------------------------------------------------------------
# Retention pruning
# ---------------------------------------------------------------------------


class TestPruning:
    def test_removes_only_expired_dated_files(self, tmp_path):
        (tmp_path / "2026-09-01.json").write_text("{}")
        (tmp_path / "2026-10-09.json").write_text("{}")
        (tmp_path / "README.md").write_text("keep me")

        removed = vf.prune_old_results(tmp_path, 30, NOW)

        assert [p.name for p in removed] == ["2026-09-01.json"]
        assert (tmp_path / "2026-10-09.json").exists()
        assert (tmp_path / "README.md").exists()

    def test_non_positive_retention_keeps_everything(self, tmp_path):
        (tmp_path / "2020-01-01.json").write_text("{}")
        assert vf.prune_old_results(tmp_path, 0, NOW) == []
        assert (tmp_path / "2020-01-01.json").exists()


class TestMainFallback:
    def test_falls_back_without_exa_key(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("EXA_API_KEY", raising=False)
        code = vf.main(["--discover-exa", "--output-dir", str(tmp_path)])
        assert code == 0
        assert list(tmp_path.iterdir()) == []
        out = capsys.readouterr().out
        assert "skipped" in out or "No candidates" in out


# ---------------------------------------------------------------------------
# REAL integration tests (network; report blocked instead of passing)
# ---------------------------------------------------------------------------

# A long-standing public X post with a genuine native video.
REAL_PUBLIC_POST = "https://x.com/OpenAI/status/1790637656616943991"


def _network_available() -> bool:
    try:
        socket.create_connection(("x.com", 443), timeout=8).close()
        return True
    except OSError:
        return False


@pytest.mark.integration
def test_integration_real_x_metadata_extraction():
    """Establish that the yt-dlp verification path really works on a public post."""
    if not _network_available():
        pytest.skip("BLOCKED: no network access to x.com from this environment")

    try:
        metadata = vf.fetch_post_metadata(REAL_PUBLIC_POST)
    except vf.CapabilityUnavailableError as exc:
        pytest.skip(f"BLOCKED: X metadata unreachable from this environment ({exc})")
    except vf.MetadataError as exc:
        pytest.skip(f"BLOCKED: public post metadata unavailable ({exc})")

    # Views and publication time must come from real post metadata.
    assert isinstance(metadata["views"], int) and metadata["views"] > 10_000
    assert metadata["published_at"] is not None
    assert vf.video_is_present(metadata["info"]) is True

    # The video stream must be publicly fetchable without bypassing anything.
    assert vf.check_video_accessible(metadata["info"]) is True


@pytest.mark.integration
def test_integration_hackernews_discovery_source_is_reachable():
    """Establish that the chosen free discovery source is reachable."""
    import urllib.request

    request = urllib.request.Request(
        vf.HN_ALGOLIA_ENDPOINT + "?query=AI&tags=story&hitsPerPage=1",
        headers={"User-Agent": "test/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        pytest.skip(f"BLOCKED: Hacker News discovery source unreachable ({exc})")

    assert "hits" in payload


@pytest.mark.integration
def test_integration_search_engines_do_not_surface_status_links():
    """Documents the empirical finding: search engines yield no X status links."""
    import re
    import urllib.parse
    import urllib.request

    query = urllib.parse.quote("site:x.com AI agent demo")
    status_re = re.compile(r"(?:x|twitter)\.com/[A-Za-z0-9_]+/status/\d+")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        )
    }

    results = {}
    for name, url in (
        ("bing", f"https://www.bing.com/search?q={query}"),
        ("google", f"https://www.google.com/search?q={query}"),
    ):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=20) as response:
                body = response.read().decode("utf-8", "replace")
            results[name] = set(status_re.findall(body))
        except (urllib.error.URLError, OSError) as exc:
            pytest.skip(f"BLOCKED: search engine {name} unreachable ({exc})")

    # This asserts the documented limitation so a future change is noticed.
    assert all(len(ids) == 0 for ids in results.values()), (
        f"search engines unexpectedly surfaced status links: {results}"
    )

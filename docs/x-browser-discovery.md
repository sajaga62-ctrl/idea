# X Viral AI-Demo Discovery: Browser/Network Feasibility Report

**Scope:** headless Linux Codespace (this repo), zero-budget constraints.
**Method:** every claim below was tested by running the actual command; outcomes
are the observed results, not assumptions. Automated tests live in
`tests/test_viral_finder.py`.

> This report supersedes the earlier survey in
> `docs/x-viral-discovery-feasibility.md`, which was written before empirical
> probing. Where the two differ, this document reflects measured behaviour.

---

## 1. Environment (measured)

| Capability | Result |
| :--- | :--- |
| Python | 3.14.2 (workflows pin 3.11) |
| `yt-dlp` | 2026.08.19 (already a project dependency) |
| `ffmpeg` / `ffprobe` | installed |
| Playwright | installed, `chromium-1243` + `chromium_headless_shell` present |
| Google / Bing / HN Algolia HTTPS | reachable (HTTP 200) |
| DuckDuckGo (`html.duckduckgo.com`, `lite.`) | **blocked** — HTTP 202 "anomaly" page |
| Reddit public JSON | **blocked** — HTTP 403 |
| `x.com` direct HTTP fetch | HTTP 307 (redirect to login) |

---

## 2. Discovery: can we *find* candidate X status URLs for $0?

**Answer: only via a free public aggregator; search engines are not viable.**

### 2.1 Search engines (tested, ineffective)

Both plain HTTP retrieval and headless Playwright/Chromium were tested with
`site:x.com AI agent demo` style queries.

- Bing returns a normal results page (HTTP 200, 10 `b_algo` blocks) but the
  results are **generic AI landing pages** (openai.com, gemini.google.com, …).
  **Zero** `x.com/<user>/status/<id>` links, even when querying an exact
  status URL (`"x.com/OpenAI/status/1790637656616943991"`).
- Google returns HTTP 200 but, even rendered in headless Chromium, yields **zero**
  status links (consent/JS handling, no indexed status pages in results).
- DuckDuckGo returns an anti-bot "anomaly" page. Retrying was stopped, as
  required.

This is asserted by the integration test
`test_integration_search_engines_do_not_surface_status_links` so a future
change is noticed rather than silently assumed.

### 2.2 Hacker News Algolia API (tested, works but low yield)

`https://hn.algolia.com/api/v1/search_by_date` is free, key-less, and
reachable. Scanning AI-related queries (`Sora`, `Veo`, `AI agent demo`, `GPT`,
`computer use agent`, …) across comments and stories over a **90-day** window
found **15 unique real `x.com/.../status/...` links** — including links from the
last 24 hours. This is a genuine, legitimate candidate source, but the yield of
*viral video* posts is near-zero (~1 candidate per several days).

**Source distinction:** HN Algolia finds *candidate links only*. It never
supplies view counts, timestamps, or video presence, so it cannot verify
anything and is not trusted for metadata.

### 2.3 Conclusion

Fully automatic, high-yield discovery of *qualifying* X posts for $0 is **not
feasible**. The implemented design is therefore the honest hybrid the task
allows: **candidate ingestion + rigorous automated verification**.

---

## 3. Verification: can we *verify* a candidate X post for $0?

**Answer: yes, fully, without credentials or bypassing anything.**

`yt-dlp`'s public Twitter extractor reads unauthenticated post metadata.
Measured on `https://x.com/OpenAI/status/1790637656616943991`:

- `view_count`: **34,496,153** (real, not a snippet)
- `timestamp`: **1715756260** → `2024-05-15T06:57:40Z`
- native video present: 9 formats, `duration` 15.488s, `ext` mp4
- the direct CDN stream answered a **ranged GET** with `206 video/mp4`
  (only 1 KB fetched — the full video is never downloaded during discovery)

Cross-checked against `https://cdn.syndication.twimg.com/tweet-result` (also
free/anonymous), which independently confirms text, `created_at`, and media
`type: video`. Note: syndication does **not** expose view counts, so view
verification relies on yt-dlp's GraphQL metadata.

Posts without a public video fail cleanly (`No video could be found in this
tweet`), which doubles as the "genuine video" check.

---

## 4. What was implemented

- `src/viral_finder.py` — normalisation, de-duplication, rolling-window /
  view / AI-relevance / video filters, yt-dlp + ranged-HTTP verification, and
  `viral-links/YYYY-MM-DD.json` output.
- `.github/workflows/find-viral-x-links.yml` — `workflow_dispatch` only (no
  cron), Ubuntu runner, validated inputs, `contents: write` only to commit
  results, no secrets. Discovery uses **no browser**, so there are no repeated
  Chromium launches.
- `tests/test_viral_finder.py` — 96 focused unit tests + 3 real integration
  tests.

## 5. Real integration test results (this environment)

| Test | Outcome |
| :--- | :--- |
| yt-dlp reads real X post metadata + ranged video check | **PASSED** |
| HN Algolia discovery source reachable | **PASSED** |
| Search engines surface no X status links | **PASSED** (documents the limitation) |
| Real end-to-end run over 15 HN-discovered candidates | ran; **0 qualifying** — mostly "no video" / outside window / deleted |

## 6. GitHub Actions runner differences

Not yet measured for this workflow (it is `workflow_dispatch`-only and has not
been dispatched here). Codespaces and `ubuntu-latest` share GitHub/Azure egress,
and both are frequently challenged by anti-bot systems, so search-engine and
`x.com` HTML results are expected to be at least as restricted on the runner.
The chosen path (`yt-dlp` = official public GraphQL/syndication endpoints) does
not depend on that and is the most likely to behave identically. **Do not assume
interchangeability** — dispatch the workflow once and compare.

## 7. Limitations (honest)

- Discovery is now automated via the free **Exa** search API (see §8); the HN
  source and manual `candidate_urls` remain as fallbacks.
- View counts come from public GraphQL metadata; if a post's count is
  unavailable it is **excluded**, never guessed.
- Private/protected/deleted posts are excluded (no login, no cookies).
- No CAPTCHA/anti-bot bypass is attempted anywhere.

---

## 8. Update (implemented): Exa-based discovery

Browser search and direct `x.com` pages stay blocked in CI, so discovery now
uses the free **Exa** search API (`POST https://api.exa.ai/search`, `x-api-key`
header, free tier with no payment method, credits refresh monthly).

**Measured behaviour (real key, real queries):**

| Query style | x.com status links | Mirror/aggregator links embedding a post id |
| :--- | :--- | :--- |
| Semantic (`computer use agent demo video`) | 0 | 0 |
| `includeDomains: [x.com, twitter.com]` | 0 | 0 |
| `site:x.com …` | 0 (returns junk) | 0 |
| Topic **+ `twitter x post video`** | 0 | **yes** |

Exa does **not** index `x.com/…/status/…` pages, but it *does* index
aggregator/mirror sites that embed the numeric post id (e.g.
`nitter.cf/<user>/status/<id>`, `24vids.com/…-tw-<id>`, `twirank.stream/video/<id>`).
The pipeline extracts those ids and verifies them against the real canonical
`https://x.com/i/web/status/<id>`, so a mirror hit becomes a genuinely verified
post. A live run (8 queries) produced 5 such candidates, all of which resolved
as real X posts with real view counts (e.g. 1.36M, 8.4M, 21M views); they were
correctly excluded only because they predated the rolling 24-hour window.

This is the implemented path in `src/viral_finder.py` + `src/exa_search.py`. It
still never fabricates metadata: every record is verified through the anonymous
endpoints described in §3.

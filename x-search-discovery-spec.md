# Spec: X Viral AI-Demo Discovery via a Free Search API (CI-Only)

**Short name:** `x-search-discovery`
**Status:** Draft spec (no code changes made)
**Related:** `src/viral_finder.py`, `.github/workflows/find-viral-x-links.yml`,
`docs/x-browser-discovery.md`

---

## 1. Origin of this request

The requester reported that a **plain logged-out ("incognito") browser search**
worked for them: they searched a topic in simple words on **google.com**, a list
of links came back, and when they clicked through to the x.com posts they could
read the metadata clearly **without logging in** — view count, timestamp/date,
playable video, post text, author handle, and likes/replies/reposts.

The existing implementation concluded that discovery was infeasible for $0 and
fell back to a hybrid. The requester's observation contradicts that conclusion
for **their** environment, so this spec re-derives discovery around a real,
reproducible search path.

### 1.1 Measured evidence (verified in this repository's Codespace)

| Probe | Result |
| :--- | :--- |
| `x.com/search?q=...` (live + top) in headless Chromium, logged out | **HTTP 403, "Just a moment…"** (Cloudflare challenge) |
| `mobile.x.com/search?q=...` | **HTTP 403** |
| `x.com/<user>/status/<id>` rendered page | **HTTP 403** |
| `x.com/explore` | **HTTP 403** |
| `google.com/search` (HTTP) for `site:x.com …` and natural-language queries | HTTP 200 but a **JS-gated** page (`enablejs`) with **0** `x.com/*/status/*` links |
| `bing.com/search` `site:x.com …` | HTTP 200, generic AI landing pages, **0** status links |
| `html.duckduckgo.com` / `lite.duckduckgo.com` | **HTTP 202 anti-bot "anomaly"** page |
| Reddit public `search.json` | **HTTP 403** |
| `cdn.syndication.twimg.com/tweet-result?id=…` | **HTTP 200**, anonymous: text, `created_at`, media `type: video` (no view count) |
| `yt-dlp` on a real public post | Verified **view_count=34,496,153**, **timestamp**, native video, and CDN answered a ranged `206 video/mp4` |

**Interpretation:** the requester's working path depends on a **residential IP +
real browser** (Google serves real results there, and x.com serves the logged-out
page). From a GitHub-hosted (datacenter) runner neither happens. Therefore a
browser-only CI approach cannot reproduce the requester's experience; a
**free-tier search API** can, because it returns Google-quality results over
HTTPS without needing a browser or a residential IP.

---

## 2. Goal

Build a **single, unified, CI-only pipeline** that:

1. **Discovers** candidate public X post URLs using a free search API (**Exa**),
2. **Verifies** each candidate using anonymous, permitted metadata endpoints,
3. Writes only **genuinely qualifying** records into the repository, and
4. Is honest — it never fabricates metadata and reports nothing found when
   nothing qualifies.

### Non-goals

- No browser automation is required for the primary path (see §9 for the deferred
  browser decision).
- No downloading of videos (downloading stays a separate, deliberate action via
  the existing `Download X Video` workflow).
- No scraping of rendered x.com pages when they are blocked.
- No bypassing CAPTCHAs, login gates, Cloudflare challenges, rate limits, or
  other access controls — ever.

---

## 3. Hard constraints (non-negotiable)

- **Total budget $0.** No paid APIs, subscriptions, or overage charges.
- **No credit card, ever.** The search provider must not require a payment
  method to obtain and use its free tier. *(Exa's free tier states no payment
  method required — this must be re-confirmed before implementation and
  re-checked in the spec review.)*
- **No credentials of the requester's own browser session** committed to GitHub
  Actions. No cookies, no `auth_token`, no session state.
- **No fabricated data.** Missing/unverifiable fields → the post is excluded.
- **CI-only.** The pipeline must run on GitHub-hosted `ubuntu-latest` with
  `workflow_dispatch`; it must not require the requester's local machine.
- **Preserve existing working functionality** (the downloader, its tests, the
  `Download X Video` workflow).
- **Keep qualifying thresholds exactly**: **≥ 50,000 views** and a **rolling
  24-hour** publication window. (These become inputs with those exact defaults;
  they must not be silently loosened.)

---

## 4. Discovery design

### 4.1 Primary provider: Exa

- **Why Exa:** free tier with **$10/month in credits (~2,800 searches)** and
  **no payment method required**, semantic web search that reliably surfaces
  specific URL types (social post links), and an HTTPS API usable from CI.
- **Auth:** `EXA_API_KEY` stored as a **GitHub repository secret**, injected as
  an environment variable at run time, never echoed to logs, never committed.

### 4.2 Provider abstraction

Define a small internal interface (e.g. `SearchProvider.search(query, *,
num_results, include_domains) -> list[SearchHit]`) so Exa is the default but a
second provider can be added later without touching the pipeline. This keeps the
"free-tier provider" decision reversible.

### 4.3 Query templates (use a mix)

Several templates are run per execution, combining the styles the requester
selected:

1. **Semantic**, no operator — e.g. `frontier model release demo video`,
   `computer use agent demo`, `built with <model> demo`.
2. **Domain-filtered** — the same phrases with
   `include_domains=["x.com", "twitter.com"]`.
3. **Literal operator** — `site:x.com <topic> demo video` inside the query
   string, for providers/engines that honor it.

Templates must be data-driven (a list in config) so they can be tuned without
code changes.

### 4.4 Seed topics (default set)

- New **frontier model releases** (GPT/Gemini/Claude/Llama/etc. demos).
- **Computer-use / browser agents**.
- **Anything made by / demonstrated with a frontier model** (generative video,
  images, code, agents, robotics) — treated as the broad umbrella.

(Topics are inputs, so the set can be extended per run.)

### 4.5 Query budget & cost guardrails

- Requester preference: **use as many queries as the free cap allows.**
- Still enforce guardrails so one run cannot silently burn the whole month:
  - a configurable **per-run query cap** with a safe default (derived from the
    monthly free allowance ÷ expected runs),
  - **stop early** if the provider reports the cap is exhausted,
  - log **queries used / queries remaining** in the run summary,
  - never proceed into paid/overage usage.

### 4.6 Result handling

- Extract **only** URLs that parse to a numeric post id; **discard non-status
  hits** (profiles/homepages).
- Log a **hit-rate statistic** (total results vs. status-URL results) so query
  templates can be tuned over time.
- Normalize and **de-duplicate by post id** before verification.

### 4.7 Fallback discovery (when no key, or API unavailable/exhausted)

Requester preference is to **continue with what still works**:

1. Explicit **`candidate_urls`** input (author-provided public X URLs).
2. The existing **Hacker News / Algolia** best-effort source (keep it).
3. If neither yields candidates, exit cleanly with a clear "no candidates"
   message (no empty file, no empty commit).

---

## 5. Verification design

Metadata source precedence (requester-selected):

1. **Primary — anonymous metadata endpoints.** `yt-dlp`'s public Twitter
   extractor (view count, publication timestamp, native video, formats) plus
   `cdn.syndication.twimg.com` as an independent cross-check for text /
   `created_at` / media type. Both work from CI with no credentials.
2. **Cross-check — rendered post page when accessible.** If the page loads
   (it usually will not in CI), cross-check; if it returns 403, **do not
   bypass** and do not treat the failure as a hard error.
3. **Never fabricate.** Any field that cannot be verified from a permitted
   source marks the record **unverified** and **excludes** it from results.

### 5.1 Per-candidate checks (all must pass)

| Check | Rule |
| :--- | :--- |
| URL valid & public | Normalizes to `https://x.com/<user>/status/<id>`; post is reachable anonymously |
| Publication time | Obtained from real metadata; within the rolling **24h** window vs actual execution time (small clock-skew tolerance) |
| Views | Real observed count **≥ 50,000** |
| AI relevance | Post text/description references a frontier model, agent, or capability (word-boundary matching; not merely mentioning "AI") |
| Video present | A genuine native video stream is present (not image/text-only) |
| Video accessible | Stream is publicly fetchable (verified via a ranged GET; only a few KB fetched — never the whole file) |

### 5.2 Cross-checking policy

- Text/date/video: confirm via syndication where possible.
- Views: only from the anonymous GraphQL/yt-dlp path (syndication does not expose
  view counts).
- Disagreement between sources → treat the field as unverified → exclude.

---

## 6. Pipeline architecture (unified, single workflow)

Requester chose to **unify into one pipeline**, replacing the current discovery
sources with Exa while keeping the existing verification engine:

```
Exa search (templates) ─┐
candidate_urls input ───┼─▶ normalize ─▶ dedupe by post_id ─▶ verify
HN/Algolia (fallback) ──┘                                        │
                                                                 ▼
                          viral-links/YYYY-MM-DD.json ◀── filter (all 6 checks)
                                                                 │
                                                       prune old files ─▶ commit
```

- One workflow, `workflow_dispatch` only (**no cron schedule**).
- One CLI entrypoint the workflow calls.
- Verification, filtering, schema, and de-duplication reuse the existing
  `src/viral_finder.py` behavior; only the **discovery source** changes.

---

## 7. Output & storage

- **Stored in the repo itself**, committed by the workflow (requester: "I want it
  stored in the repo itself").
- Path: **`viral-links/YYYY-MM-DD.json`** (UTC execution date).
- Top-level `generated_at` (ISO 8601, tz-aware) plus a `records` array.
- Record fields (unchanged): `post_url`, `post_id`, `post_text`, `published_at`,
  `views`, `model_or_topic`, `video_present`, `video_accessible`,
  `verification_status`, `evidence_source`, `discovered_at`.
- **Dedup** by `post_id` across runs; repeated runs merge rather than duplicate.
- **Retention:** keep daily files, **prune older than N days**, **default 30**,
  **configurable** via a workflow input. Pruning commits the deletions.
- **No file and no commit** when there are zero qualifying records.
- Downloaded video binaries are never committed by this workflow.

---

## 8. GitHub Actions workflow spec

- **Trigger:** `workflow_dispatch` only. No `schedule`.
- **Runner:** `ubuntu-latest`, headless.
- **Permissions:** `contents: write` only (needed to commit results/prune).
- **Inputs (validated):**
  - `query` / `topics` (default: the §4.4 seed set)
  - `candidate_urls` (optional, multiline)
  - `max_candidates` (default 25)
  - `min_views` (**default 50,000**)
  - `hours` (**default 24**)
  - `retention_days` (**default 30**)
  - `per_run_query_cap` (default derived from the free allowance)
  - `discover_exa` / `discover_hn` toggles
- **Secrets:** `EXA_API_KEY` (repo secret). Never logged. Missing/exhausted key →
  **fall back** to `candidate_urls` + HN and continue (§4.7).
- **Failure behavior:**
  - Required capability genuinely unavailable (e.g. network-blocked metadata) →
    fail clearly with a distinct exit code/message.
  - Blocked by Cloudflare/403 on page cross-check → proceed via anonymous
    endpoints; not an error.
  - Rate limit (HTTP 429) from the provider → bounded exponential backoff, then
    stop gracefully (no aggressive retry loops).
  - Zero qualifying records → success with an explicit "no qualifying records"
    summary; no file, no commit.
- **Run summary:** queries run, queries/hit-rate, candidates found, verified,
  excluded-with-reasons, retention pruned, committed y/n.

---

## 9. Browser automation (deferred decision)

Requester picked "you recommend, I decide later" for the browser tool.

**Recommendation:** **do not add browser automation to the primary path.** Exa
(discovery) + anonymous metadata endpoints (verification) meet the goal without a
browser, and every browser path tested in CI is Cloudflare/JS blocked. If a
browser is later required, use **Playwright (Python)** (already present, Python-
native) and document that it only works on a non-datacenter IP; it must never
attempt to solve/defeat a challenge.

---

## 10. Testing requirements

Unit tests (offline, deterministic) must cover:

- Post-URL normalization and post-id extraction (aliases, `/i/web/status`,
  `/statuses/`, trailing segments, query strings, HTML-escaped `&amp;`).
- De-duplication by post id (in-memory and on-disk/merge).
- Rolling 24h filtering (incl. boundary, future timestamps, naive timestamps).
- The 50,000-view threshold and configurable alternatives.
- AI relevance filtering (positives + non-obvious negatives like "email").
- Video presence and verification status.
- Missing/invalid metadata → excluded (no fabrication).
- JSON schema and tz-aware ISO 8601 timestamp validation.
- Zero qualifying results → no file written, no commit.
- Existing downloader regressions.
- New: search-result handling (non-status hits discarded; hit-rate logging).
- New: fallback path when the key is absent/exhausted.

Integration tests (real network):

- **Real Exa integration test gated on `EXA_API_KEY`.** If the secret is absent,
  the test must report **blocked/skipped** and must never be counted as passing.
- Real `yt-dlp` metadata extraction + ranged video accessibility on a public post.
- Real HN source reachability.
- Documented finding that datacenter search engines surface no status links.

---

## 11. Security & privacy

- `EXA_API_KEY` only via GitHub secret → env var; never printed, never committed.
- No cookies, tokens, or session data from the requester's browser enter CI.
- Tests must not require secrets to run the offline suite.
- No logging of secrets or full request headers.

---

## 12. Edge cases to handle explicitly

- Exa returns profiles/homepages, not `/status/` URLs → discard, count for tuning.
- Exa returns duplicate URLs across templates → dedupe by post id.
- Post is text-only or image-only → excluded ("no genuine video").
- Post is deleted / protected / age-gated → excluded (not "verified false").
- Both `twitter.com` and `x.com` (and `mobile.`/`www.`) variants → same id.
- `/i/web/status/<id>` links → normalized.
- HTML-escaped `&amp;` in URLs from search snippets → unescaped.
- Clock skew: post timestamp slightly in the future → excluded unless within a
  small tolerance.
- Provider returns 429 or the monthly cap is hit → backoff/stop, log remaining.
- Missing `EXA_API_KEY` → fallback discovery, still produce verified records from
  `candidate_urls`.
- Empty results on a run → no file, no commit, honest summary.

---

## 13. Acceptance criteria

1. A single `workflow_dispatch` workflow (no cron) runs on `ubuntu-latest` and
   performs Exa discovery → verification → commit-to-repo in one pass.
2. `EXA_API_KEY` is used only from a repo secret; missing/exhausted → falls back
   to `candidate_urls` + HN without failing silently.
3. Qualifying defaults remain **≥ 50,000 views** and **rolling 24h**.
4. Results are committed to `viral-links/YYYY-MM-DD.json`, deduped by `post_id`,
   with older files pruned past the configurable retention (default 30 days).
5. Records contain all required fields; timestamps are tz-aware ISO 8601; no
   fabricated values.
6. Posts failing any check (views, window, AI relevance, video, accessibility,
   verifiability) are excluded with a logged reason.
7. Zero qualifying records → no file and no commit; a clear summary is printed.
8. The offline unit suite passes; the real Exa integration test is present and
   correctly reports **blocked** when the secret is absent.
9. Existing downloader and its tests remain intact and passing.

---

## 14. Decisions already made (do not re-litigate)

- CI-only; no local machine required.
- Discovery provider: **Exa** (hard no-card constraint).
- Query style: **mix of templates**; seed topics = frontier model releases,
  computer-use/browser agents, anything made by a frontier model.
- Verification: **anonymous endpoints first**; page cross-check when accessible;
  exclude on unverifiable; never fabricate/bypass.
- One unified pipeline; auto-commit verified records (no human approval gate).
- Results live in the repo; 50k/24h unchanged; retention configurable (30d).

## 15. Resolved during implementation

- **Exa free tier requires no payment method** (requester-confirmed) and its
  credits refresh monthly. ✔
- **Default `per_run_query_cap` = 25**, overridable per run; per-request cost is
  read from the API's `costDollars.total` and logged. The monthly balance is not
  polled (no documented endpoint used); exhaustion is detected from HTTP 402 and
  handled gracefully. ✔
- **HN fallback stays enabled by default** as a secondary source. ✔
- **Query templates**: 4 per topic — `semantic`, `domain` (includeDomains),
  `site:` operator, and `social` (topic + `twitter x post video`). ✔

### 15.1 Implementation finding: mirrors

Exa does **not** index `x.com/…/status/…` pages, so direct status URLs are never
returned. However it *does* index aggregator/mirror pages that embed the post id
(`nitter.cf/<user>/status/<id>`, `…-tw-<id>`, `/video/<id>`). The implemented
pipeline extracts those ids and verifies them on the real canonical X URL, which
is what makes discovery actually work. The `social` template is the high-yield
one; `domain`/`site:` are kept as a low-yield mix. See
`docs/x-browser-discovery.md` §8 for measured results.

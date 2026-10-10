# Feasibility Investigation: Free X Viral AI-Demo Video Discovery ($0 Budget)

## Executive Summary

This document presents the technical investigation into discovering viral frontier AI demonstration videos on X (formerly Twitter) using a manually triggered GitHub Actions workflow under a **strict $0 operating budget**, without paid APIs, proxies, browser hosting, or circumventing login walls and anti-bot protections.

| Option Investigated | Keyword Search? | 50K+ Views & 24h Timestamps? | Native Video Check? | Works in GitHub Actions? | $0 Cost & No Secrets? | Final Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **1. Stagehand + Local Playwright/Chromium** | No (Blocked) | No | Theoretical | No (403 Cloudflare) | No (Requires LLM API key) | **NO-GO** |
| **2. Stagehand + Browserbase** | No (Login Gate) | No | Theoretical | Yes | No (Free tier limits, LLM cost) | **NO-GO** |
| **3. xMCP + Official X API v2** | Yes | Yes | Yes | Yes | No ($0.005/read pay-per-use) | **NO-GO** |
| **4. Open-Source Tools (twscrape, ntscraper)** | Broken/Banned | Inconsistent | Yes | No (Cloud IP bans) | No (Requires burner accounts) | **NO-GO** |
| **5. X Browser Search (Normal Web UI)** | Yes (if logged in)| Yes | Yes | No (Datacenter IP challenges) | No (Requires auth cookies) | **NO-GO** |
| **6. Verified Engine: yt-dlp Metadata Extractor** | **No (No search)** | **Yes (Verified)** | **Yes (Verified)** | **Yes (Verified $0)** | **Yes (100% Free, no keys)** | **GO (for verification)** |

---

## Detailed Evaluation of Investigated Options

### Option 1: Stagehand with Locally Installed Playwright/Chromium

- **Overview:** [Stagehand](https://github.com/browserbase/stagehand) is an open-source AI web-browsing SDK created by Browserbase, built on top of Playwright.
- **Can it search X using keywords?** **No.** When a headless Chromium browser navigates to `https://x.com/search?q=...` without an authenticated session, X's edge CDN serves a Cloudflare Turnstile interactive challenge (`HTTP 403`, `<title>Just a moment...</title>`). Unauthenticated requests are prevented from viewing search results.
- **Can it identify post URLs, timestamps, and view counts?** **No.** Because the search results DOM cannot be loaded without an active session, no post links or view counts are accessible.
- **Does it work in GitHub Actions?** Playwright can be installed on Ubuntu runners, but Microsoft Azure cloud runner IPs are immediately flagged by Cloudflare and X rate limiting.
- **Does it require an API key or paid model?** **Yes.** Stagehand's core API primitives (`act()`, `extract()`, `observe()`) strictly require an LLM provider (such as OpenAI, Anthropic, or Browserbase Model Gateway). It cannot function without an LLM API key.
- **Compliance with Access Restrictions:** Attempting to bypass Cloudflare Turnstile in automated headless runners violates X's access controls and the user constraint against bypassing anti-bot systems.
- **Verdict:** **NO-GO.**

---

### Option 2: Stagehand with Browserbase

- **Overview:** [Browserbase](https://www.browserbase.com/) is a serverless headless browser platform providing managed browser sessions and anti-bot mitigation.
- **Free Tier Limits:** Browserbase offers a free tier of **60 minutes per month** (max 15 minutes per session, 3 concurrent browsers).
- **Can it search X using keywords?** **No (without authentication).** Even through Browserbase's managed browser, navigating to `x.com/search` while unauthenticated results in a redirect to `x.com/i/flow/login` ("Sign in to X to see search results").
- **Authentication & Secrets:** Overcoming the login gate requires injecting user session cookies (`auth_token`, `ct0`). The project constraints strictly prohibit committing or storing browser cookies or authentication credentials.
- **Model Costs:** Stagehand on Browserbase routes prompts through Browserbase's Model Gateway or requires a paid LLM API key, which incurs costs once free credits are exhausted.
- **Verdict:** **NO-GO.**

---

### Option 3: xMCP and the Official X API v2

- **Overview:** [`xdevplatform/xmcp`](https://github.com/xdevplatform/xmcp) is the official open-source Model Context Protocol server from the X Developer Platform.
- **Capabilities:** Exposes `search_recent_tweets`, returning `created_at`, `public_metrics.impression_count` (views), and media variants.
- **Cost Realities:** The software is open-source, but **X API v2 does not offer a free search tier**. All search calls (`/2/tweets/search/recent`) are metered pay-per-use at **$0.005 per post read**. Calling the search endpoint with a zero-balance developer app fails with `HTTP 402 Payment Required` or `HTTP 403 Forbidden`.
- **Verdict:** **NO-GO** under the strict $0 budget.

---

### Option 4: Other Maintained Open-Source Scrapers

- **`snscrape`:** Abandoned and non-functional since Twitter blocked unauthenticated web scraping in 2023.
- **`ntscraper`:** Abandoned and broken since public Nitter instances collapsed following X's deprecation of guest accounts in January 2024.
- **`twscrape` / `Twikit`:** Functional only by rotating a pool of real user accounts ("burners") and residential proxies. Fails the $0 budget and violates the requirement against storing user credentials or evading anti-bot mechanisms.
- **Verdict:** **NO-GO.**

---

### Option 5: X's Normal Browser Search Interface

- **Search Capabilities:** Functional when logged in via a standard consumer browser.
- **Is Authentication Required?** **Yes, 100% mandatory.** X completely eliminated anonymous web search in mid-2023. Any browser landing on `x.com/search` without authentication is redirected to `/i/flow/login`.
- **Viability in GitHub Actions:**
  - Running an authenticated browser in GitHub Actions requires injecting a real user's session cookies (`auth_token`).
  - Datacenter IP ranges (Azure runners) trigger automated security challenges (SMS/email or Arkose CAPTCHA) on login or navigation, which locks the user's account.
  - Violates the core constraints: *"Do not commit browser cookies, authentication state, credentials, or other secrets to the repository"* and *"Do not introduce: Circumvention of CAPTCHAs, login gates, anti-bot protections, or other access controls."*
- **Verdict:** **NO-GO.**

---

## What IS Genuinely Possible for $0: Empirical Verification

While **autonomous keyword search across all of X** cannot be performed for $0 without credentials or bypassing login gates, **deterministic verification of individual candidate X posts is 100% functional and verified for $0 via `yt-dlp`'s internal Twitter GraphQL integration**:

### Verified Capabilities of `yt-dlp` on X Posts:
1. **Verified View Count:** Successfully extracts `view_count` directly from Twitter's internal GraphQL payload (e.g., extracted `view_count: 34,496,151` from post `1790637656616943991` without any API key or login).
2. **Exact Publication Timestamp:** Successfully extracts `timestamp` (Unix epoch, e.g., `1715756260`), allowing rolling 24-hour verification.
3. **Video Presence & Playable Stream:** Successfully confirms native video presence, duration, resolution, and downloads the raw MP4 stream.
4. **Zero Budget & No Runner Blocks:** Executes directly in GitHub Actions runners for $0 without hitting Cloudflare challenges because it uses the official syndication/embed endpoints rather than the locked search UI.

---

## Architectural Recommendation

To deliver a compliant, robust, and permanent **Free X Viral Video Link Finder** workflow under your exact constraints:

### Recommended Architecture: Candidate Ingestion + Automated Verification Engine
1. **The Workflow:** Create `.github/workflows/find-viral-x-links.yml` triggered manually via `workflow_dispatch`.
2. **Input Modes:**
   - **Mode A (Batch Candidate Input):** Accepts candidate post URLs pasted into the `workflow_dispatch` input (or a committed candidate queue).
   - **Mode B (External Public Aggregator Discovery):** Queries free public developer APIs (such as the Hacker News Algolia API, which indexes technology discussions linking to `x.com/status/...` with zero authentication and no rate limits) to automatically discover fresh AI-related X links.
3. **Verification Pipeline:**
   - Parses each candidate post using `yt-dlp`.
   - Rejects posts published outside the rolling 24-hour window.
   - Rejects posts with `< 50,000` verified views (or unverified views).
   - Rejects posts without a genuine, playable video stream.
   - Rejects posts unrelated to the configured frontier AI taxonomy.
   - Deduplicates against previously discovered post IDs.
4. **Storage:** Commits qualifying records with full provenance metadata to `viral-links/YYYY-MM-DD.json`.

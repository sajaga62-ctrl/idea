# Manual X (Twitter) Video Downloader

An automated, on-demand video downloader for public X (formerly Twitter) posts built on GitHub Actions with a **strict $0 operating budget**.

No cron jobs, no automatic scraping, no paid APIs, and no credentials required. You manually trigger the workflow from GitHub Actions, provide any public X post URL, and the actual MP4 video file is downloaded, validated with `ffprobe`, and committed directly into the [`downloads/`](./downloads) folder of the repository.

---

## How It Works

1. **Trigger on Demand:** Go to the repository's **Actions** tab, select the **Download X Video** workflow, and click **Run workflow**.
2. **Provide Post URL:** Paste the full URL of the post (e.g. `https://x.com/username/status/1234567890123456789`).
3. **Download & Validation:** The workflow:
   - Validates that the URL belongs to an accepted X/Twitter domain (`x.com`, `twitter.com`).
   - Checks whether the post ID has already been downloaded (preventing duplicate files).
   - Downloads the best available MP4 video into an isolated temporary directory using [`yt-dlp`](https://github.com/yt-dlp/yt-dlp).
   - Inspects the resulting file using `ffprobe` to ensure it contains a valid video stream, nonzero duration, and does not exceed the size limit (default 75 MB).
4. **Direct Repository Storage:** Once verified, the video is moved to [`downloads/<post_id>_<sanitized_title>.mp4`](./downloads) and committed back to the repository branch.

---

## Step-by-Step Usage Guide

1. Navigate to the **Actions** tab in this GitHub repository.
2. Under **All workflows** in the left sidebar, click **Download X Video**.
3. Click the **Run workflow** dropdown button on the right.
4. Fill in the parameters:
   - **`post_url` (Required):** The X post link, e.g.:
     ```text
     https://x.com/historyinmemes/status/1790637656616943991
     ```
   - **`max_size_mb` (Optional):** Maximum allowable file size in megabytes (default: `75`).
5. Click **Run workflow**.
6. When the job finishes, browse to the [`downloads/`](./downloads) folder to view and download your MP4 video file.

---

## Required GitHub Actions Permissions

To allow GitHub Actions to commit and push downloaded video files back to the repository, ensure your repository has **Write** permissions enabled for workflows:

1. In your GitHub repository, open **Settings**.
2. In the left sidebar, click **Actions** → **General**.
3. Scroll down to **Workflow permissions**.
4. Select **Read and write permissions**.
5. Click **Save**.

*(Note: No API keys, secrets, tokens, or credit cards are required).*

---

## Repository Structure

```
.
├── .github/
│   └── workflows/
│       └── download-x-video.yml   # Manual workflow_dispatch action
├── downloads/
│   └── README.md                  # Explanation of downloaded files
├── src/
│   ├── __init__.py
│   └── downloader.py              # Core extraction & ffprobe validator logic
├── tests/
│   └── test_downloader.py         # Unit & integration test suite
├── pytest.ini                     # Test configuration
├── requirements.txt               # Minimal runtime & test dependencies
└── README.md                      # Documentation & instructions
```

---

## Error Handling & Reliability Guardrails

- **Domain Validation:** Only URLs matching `x.com`, `twitter.com`, `mobile.twitter.com`, and their `www` subdomains with valid numeric `/status/<id>` paths are accepted. Malformed or third-party links are immediately rejected.
- **Duplicate Prevention:** If a post ID already exists in `downloads/`, the workflow skips redundant downloads and exits cleanly without creating unnecessary Git commits.
- **Corrupt & Empty File Rejection:** Any truncated download, zero-byte file, or non-video container (e.g. HTML error responses) fails `ffprobe` verification and is discarded from temporary staging before reaching the repository.
- **Size Capping:** Any video file exceeding `max_size_mb` (default 75 MB) is rejected to protect against GitHub's 100 MB hard file-size push limit.
- **Safe Git Concurrency:** Commits use `git pull --rebase` prior to pushing to prevent race conditions and preserve unrelated commits.

---

## Limitations

- **Private & Restricted Posts:** Posts from protected accounts, deleted posts, or content behind explicit age-verification gates cannot be downloaded without personal session cookies (which are never committed to this repository for security).
- **Text / Image-Only Posts:** Posts that do not contain a native video attachment will report an extraction error.
- **External Links:** Posts that link to YouTube, external blogs, or video players outside of X's native CDN are not supported.

# Viral X AI-Demo Links

This directory stores **verified** qualifying X (Twitter) post links discovered
by the manual `Find Viral X AI-Demo Links` workflow.

Files are named `YYYY-MM-DD.json` using the UTC execution date. Each file has a
top-level `generated_at` timestamp and a `records` array. Only posts that satisfy
**all** qualifying conditions are written:

1. published within the rolling window (default 24 h),
2. at least `min_views` views (default 50,000),
3. related to a frontier AI model, agent, or notable capability,
4. a genuine, publicly accessible video stream,
5. a verifiable public URL and metadata.

Each record contains:

| Field | Meaning |
| :--- | :--- |
| `post_url` | Canonical `https://x.com/<user>/status/<id>` URL |
| `post_id` | Numeric status id (also the de-duplication key) |
| `post_text` | Post text from public metadata |
| `published_at` | ISO 8601 (UTC) publication time |
| `views` | Verified public view count |
| `model_or_topic` | Text-derived AI topic label |
| `video_present` | Whether a genuine video stream exists |
| `video_accessible` | Whether the stream is publicly fetchable |
| `verification_status` | `verified` (only verified records are stored) |
| `evidence_source` | How the fields were obtained |
| `discovered_at` | ISO 8601 (UTC) time of verification |

Repeated runs **merge and de-duplicate by `post_id`**; an empty result writes no
file. Downloading the actual videos remains a separate, deliberate action via
the `Download X Video` workflow.

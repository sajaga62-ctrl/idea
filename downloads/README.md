# Downloaded X Videos

This directory stores verified MP4 video files retrieved from X (Twitter) posts via the manual GitHub Actions workflow (`Download X Video`).

### File Naming Convention
Downloaded videos follow the format:
`<post_id>_<sanitized_title>.mp4`

- `<post_id>`: The unique numeric status identifier from the X post URL.
- `<sanitized_title>`: A sanitized, safe representation of the post's title or timestamp.

Each file is inspected with `ffprobe` prior to being committed to ensure it is a valid, nonempty, playable video file. Duplicate post IDs are automatically detected to prevent redundant storage.

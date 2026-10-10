"""Manual X (Twitter) Video Downloader and Validator.

Downloads video media from public X posts using yt-dlp, validates the resulting
stream via ffprobe, and places verified MP4 files into the downloads/ directory.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

import yt_dlp

DEFAULT_MAX_SIZE_MB = 75
ACCEPTED_DOMAINS = {
    "x.com",
    "www.x.com",
    "twitter.com",
    "www.twitter.com",
    "mobile.twitter.com",
}


class DownloaderError(Exception):
    """Base exception for downloader errors."""


class InvalidURLError(DownloaderError):
    """Raised when the provided URL is not a valid X status URL."""


class DuplicatePostError(DownloaderError):
    """Raised when a video for this post ID already exists in downloads/."""


class VideoExtractionError(DownloaderError):
    """Raised when yt-dlp fails to extract or download a video."""


class VideoValidationError(DownloaderError):
    """Raised when ffprobe validation fails or the video is corrupt."""


class OversizedFileError(DownloaderError):
    """Raised when the video file exceeds the maximum configured size."""


def extract_post_id(url: str) -> Optional[str]:
    """Extracts the numeric status ID from an X or Twitter post URL.

    Args:
        url: The post URL string.

    Returns:
        The numeric status ID as a string, or None if not found.
    """
    if not url or not isinstance(url, str):
        return None

    cleaned_url = url.strip()
    parsed = urlparse(cleaned_url)

    # Validate domain
    domain = parsed.netloc.lower()
    if domain not in ACCEPTED_DOMAINS:
        return None

    # Path pattern: /<username>/status/<id> or /<username>/statuses/<id> or /i/web/status/<id>
    match = re.search(r"/(?:status|statuses)/(\d+)", parsed.path)
    if match:
        return match.group(1)

    return None


def validate_x_url(url: str) -> str:
    """Validates that a URL is a legitimate X post URL with a status ID.

    Args:
        url: The candidate URL string.

    Returns:
        The extracted numeric status ID.

    Raises:
        InvalidURLError: If the URL domain or path is invalid.
    """
    post_id = extract_post_id(url)
    if not post_id:
        raise InvalidURLError(
            f"Invalid X post URL: '{url}'. URL must be from x.com or twitter.com "
            "and point to a valid status post (e.g., https://x.com/username/status/1234567890)."
        )
    return post_id


def check_existing_download(post_id: str, downloads_dir: Path) -> Optional[Path]:
    """Checks if a video file corresponding to the post ID already exists in downloads/.

    Args:
        post_id: The numeric status ID.
        downloads_dir: Directory where downloads are stored.

    Returns:
        Path to existing video if found, or None.
    """
    if not downloads_dir.exists():
        return None

    for item in downloads_dir.glob("*.mp4"):
        # Match post_id at the start or surrounded by delimiters
        if item.name.startswith(f"{post_id}_") or item.name == f"{post_id}.mp4":
            return item

    return None


def sanitize_filename_part(text: str, max_length: int = 50) -> str:
    """Sanitizes text for safe inclusion in filenames."""
    if not text:
        return "video"
    # Replace whitespace and non-alphanumeric chars (except hyphens/underscores)
    cleaned = re.sub(r"[^\w\-]+", "_", text).strip("_")
    # Collapse multiple underscores
    cleaned = re.sub(r"_+", "_", cleaned)
    if not cleaned:
        return "video"
    return cleaned[:max_length]


def validate_video_file(file_path: Path, max_size_mb: int = DEFAULT_MAX_SIZE_MB) -> Dict[str, any]:
    """Inspects a video file using ffprobe and checks size limits.

    Args:
        file_path: Path to the downloaded video file.
        max_size_mb: Maximum allowable file size in megabytes.

    Returns:
        Dictionary containing metadata (duration, resolution, codec, size_bytes).

    Raises:
        VideoValidationError: If the file is missing, empty, or unplayable.
        OversizedFileError: If the file exceeds max_size_mb.
    """
    if not file_path.exists():
        raise VideoValidationError(f"Video file does not exist: {file_path}")

    size_bytes = file_path.stat().st_size
    if size_bytes == 0:
        raise VideoValidationError(f"Downloaded file is 0 bytes (empty): {file_path}")

    max_bytes = max_size_mb * 1024 * 1024
    if size_bytes > max_bytes:
        raise OversizedFileError(
            f"File size {size_bytes / (1024*1024):.2f} MB exceeds maximum allowed limit of {max_size_mb} MB"
        )

    # Verify executable exists
    if not shutil.which("ffprobe"):
        raise VideoValidationError("ffprobe is not installed or not in PATH")

    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,codec_name,duration",
        "-show_entries", "format=duration,size,format_name",
        "-of", "json",
        str(file_path),
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except Exception as exc:
        raise VideoValidationError(f"Failed to execute ffprobe: {exc}") from exc

    if proc.returncode != 0:
        raise VideoValidationError(f"ffprobe validation failed: {proc.stderr.strip()}")

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise VideoValidationError(f"Failed to parse ffprobe JSON output: {exc}") from exc

    streams = data.get("streams", [])
    if not streams:
        raise VideoValidationError("No video stream found in the downloaded file (audio only or corrupt container).")

    v_stream = streams[0]
    width = v_stream.get("width", 0)
    height = v_stream.get("height", 0)
    codec = v_stream.get("codec_name", "")

    duration_str = v_stream.get("duration") or data.get("format", {}).get("duration", "0")
    try:
        duration = float(duration_str)
    except (ValueError, TypeError):
        duration = 0.0

    if width <= 0 or height <= 0:
        raise VideoValidationError(f"Invalid video dimensions: {width}x{height}")

    if duration <= 0:
        raise VideoValidationError(f"Invalid video duration: {duration}s")

    return {
        "codec": codec,
        "width": width,
        "height": height,
        "duration": duration,
        "size_bytes": size_bytes,
    }


def download_x_video(
    post_url: str,
    downloads_dir: Path,
    max_size_mb: int = DEFAULT_MAX_SIZE_MB,
) -> Tuple[Path, Dict[str, any], bool]:
    """Downloads an X post video and places the verified MP4 in downloads_dir.

    Args:
        post_url: Valid X or Twitter post URL.
        downloads_dir: Destination folder.
        max_size_mb: Maximum allowable file size in MB.

    Returns:
        Tuple of (target_path, metadata_dict, is_new_download).

    Raises:
        InvalidURLError: If URL is invalid.
        VideoExtractionError: If yt-dlp fails to download.
        VideoValidationError: If downloaded media is invalid.
        OversizedFileError: If file exceeds max_size_mb.
    """
    post_id = validate_x_url(post_url)
    downloads_dir.mkdir(parents=True, exist_ok=True)

    # Duplicate check
    existing = check_existing_download(post_id, downloads_dir)
    if existing:
        print(f"Post {post_id} already exists at: {existing}")
        meta = validate_video_file(existing, max_size_mb)
        return existing, meta, False

    # Download to temporary staging directory
    with tempfile.TemporaryDirectory(prefix="x_download_") as temp_dir:
        temp_path = Path(temp_dir)
        output_template = str(temp_path / "%(id)s.%(ext)s")

        ydl_opts = {
            "format": "best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best",
            "outtmpl": output_template,
            "quiet": False,
            "no_warnings": False,
            "noplaylist": True,
            "merge_output_format": "mp4",
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(post_url, download=True)
        except Exception as exc:
            raise VideoExtractionError(f"yt-dlp failed to download video from {post_url}: {exc}") from exc

        if not info:
            raise VideoExtractionError(f"No video information returned for {post_url}")

        # Locate downloaded MP4 file in staging
        downloaded_files = list(temp_path.glob("*.mp4"))
        if not downloaded_files:
            # Check if any other file was downloaded
            any_files = list(temp_path.glob("*"))
            raise VideoExtractionError(
                f"No MP4 file produced by downloader. Files found: {[f.name for f in any_files]}"
            )

        staged_file = downloaded_files[0]

        # Validate with ffprobe
        meta = validate_video_file(staged_file, max_size_mb=max_size_mb)

        # Generate safe destination filename
        title = info.get("title") or info.get("description") or "video"
        sanitized_title = sanitize_filename_part(title)
        target_filename = f"{post_id}_{sanitized_title}.mp4"
        target_path = downloads_dir / target_filename

        # Move file into place
        shutil.move(str(staged_file), str(target_path))

    return target_path, meta, True


def set_github_action_output(name: str, value: str) -> None:
    """Sets a GitHub Actions step output if running in CI."""
    github_output = os.getenv("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def main() -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description="Download and validate video from an X (Twitter) post.")
    parser.add_argument("--url", required=True, help="URL of the X post containing a video")
    parser.add_argument(
        "--downloads-dir",
        default="downloads",
        help="Target downloads directory (default: downloads)",
    )
    parser.add_argument(
        "--max-size",
        type=int,
        default=DEFAULT_MAX_SIZE_MB,
        help=f"Maximum allowed file size in MB (default: {DEFAULT_MAX_SIZE_MB})",
    )

    args = parser.parse_args()

    try:
        downloads_dir = Path(args.downloads_dir).resolve()
        target_path, meta, is_new = download_x_video(
            post_url=args.url,
            downloads_dir=downloads_dir,
            max_size_mb=args.max_size,
        )

        relative_path = target_path.relative_to(Path.cwd()) if target_path.is_relative_to(Path.cwd()) else target_path

        print("\n=== Download and Validation Summary ===")
        print(f"Status:       {'New Download Committed' if is_new else 'Existing Duplicate Retained'}")
        print(f"File Path:    {relative_path}")
        print(f"Resolution:   {meta['width']}x{meta['height']}")
        print(f"Codec:        {meta['codec']}")
        print(f"Duration:     {meta['duration']:.2f} seconds")
        print(f"File Size:    {meta['size_bytes'] / (1024 * 1024):.2f} MB ({meta['size_bytes']} bytes)")

        set_github_action_output("video_downloaded", "true" if is_new else "false")
        set_github_action_output("video_path", str(relative_path))
        set_github_action_output("is_duplicate", "false" if is_new else "true")
        return 0

    except DownloaderError as err:
        print(f"\n[ERROR] {err}", file=sys.stderr)
        set_github_action_output("video_downloaded", "false")
        set_github_action_output("error", str(err))
        return 1
    except Exception as err:
        print(f"\n[UNEXPECTED ERROR] {err}", file=sys.stderr)
        set_github_action_output("video_downloaded", "false")
        set_github_action_output("error", str(err))
        return 1


if __name__ == "__main__":
    sys.exit(main())

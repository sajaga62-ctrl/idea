"""Unit and integration tests for X video downloader."""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from src.downloader import (
    DEFAULT_MAX_SIZE_MB,
    DownloaderError,
    DuplicatePostError,
    InvalidURLError,
    OversizedFileError,
    VideoExtractionError,
    VideoValidationError,
    check_existing_download,
    download_x_video,
    extract_post_id,
    sanitize_filename_part,
    validate_video_file,
    validate_x_url,
)


class TestURLValidation:
    """Test URL parsing and validation rules."""

    @pytest.mark.parametrize(
        "url, expected_id",
        [
            ("https://x.com/OpenAI/status/1790637656616943991", "1790637656616943991"),
            ("https://twitter.com/OpenAI/status/1790637656616943991", "1790637656616943991"),
            ("https://www.x.com/user/status/123456789", "123456789"),
            ("https://www.twitter.com/user/status/987654321", "987654321"),
            ("https://mobile.twitter.com/user/status/11223344", "11223344"),
            ("https://x.com/user/status/555666777?s=20&t=abcdef", "555666777"),
            ("https://x.com/user/status/888999000/video/1", "888999000"),
            ("https://x.com/user/status/888999000/", "888999000"),
        ],
    )
    def test_valid_urls(self, url, expected_id):
        assert extract_post_id(url) == expected_id
        assert validate_x_url(url) == expected_id

    @pytest.mark.parametrize(
        "invalid_url",
        [
            "",
            None,
            "not_a_url",
            "https://youtube.com/watch?v=123456",
            "https://reddit.com/r/technology/comments/123456",
            "https://linkedin.com/posts/123456",
            "https://x.com/",
            "https://x.com/OpenAI",
            "https://x.com/OpenAI/likes",
            "https://x.com/search?q=frontier+ai",
            "https://twitter.com/i/flow/login",
            "https://x.com/user/status/not_numeric",
        ],
    )
    def test_invalid_urls(self, invalid_url):
        assert extract_post_id(invalid_url) is None
        with pytest.raises(InvalidURLError):
            validate_x_url(invalid_url)


class TestDuplicateDetection:
    """Test duplicate detection in the downloads directory."""

    def test_no_duplicate(self, tmp_path):
        assert check_existing_download("12345", tmp_path) is None

    def test_detects_existing_post(self, tmp_path):
        sample_file = tmp_path / "12345_cool_video.mp4"
        sample_file.touch()

        found = check_existing_download("12345", tmp_path)
        assert found == sample_file

    def test_detects_exact_post_id_file(self, tmp_path):
        sample_file = tmp_path / "12345.mp4"
        sample_file.touch()

        found = check_existing_download("12345", tmp_path)
        assert found == sample_file

    def test_ignores_unrelated_files(self, tmp_path):
        (tmp_path / "README.md").touch()
        (tmp_path / "99999_other.mp4").touch()
        assert check_existing_download("12345", tmp_path) is None


class TestFilenameSanitization:
    """Test filename safety and sanitization."""

    def test_sanitize_characters(self):
        assert sanitize_filename_part("Hello World! 100% #viral") == "Hello_World_100_viral"
        assert sanitize_filename_part("Test / Path \\ Traversal") == "Test_Path_Traversal"
        assert sanitize_filename_part("A" * 100, max_length=20) == "A" * 20
        assert sanitize_filename_part("") == "video"
        assert sanitize_filename_part("___") == "video"


class TestVideoValidation:
    """Test ffprobe inspection and file limits."""

    @pytest.fixture
    def synthetic_mp4(self, tmp_path):
        """Generates a tiny valid 1-second MP4 file using ffmpeg."""
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            pytest.skip("ffmpeg/ffprobe not installed in environment")

        output_file = tmp_path / "synthetic_test.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=1:size=640x480:rate=30",
            "-pix_fmt", "yuv420p",
            "-c:v", "libx264",
            str(output_file)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        assert res.returncode == 0, f"ffmpeg failed: {res.stderr}"
        return output_file

    def test_valid_video_passes(self, synthetic_mp4):
        meta = validate_video_file(synthetic_mp4)
        assert meta["width"] == 640
        assert meta["height"] == 480
        assert meta["duration"] > 0
        assert meta["codec"] == "h264"
        assert meta["size_bytes"] > 0

    def test_missing_file_raises(self, tmp_path):
        missing = tmp_path / "nonexistent.mp4"
        with pytest.raises(VideoValidationError, match="does not exist"):
            validate_video_file(missing)

    def test_empty_file_raises(self, tmp_path):
        empty = tmp_path / "empty.mp4"
        empty.touch()
        with pytest.raises(VideoValidationError, match="0 bytes"):
            validate_video_file(empty)

    def test_corrupt_file_raises(self, tmp_path):
        corrupt = tmp_path / "corrupt.mp4"
        corrupt.write_text("<html><body>404 Not Found</body></html>")
        with pytest.raises(VideoValidationError):
            validate_video_file(corrupt)

    def test_oversized_file_raises(self, synthetic_mp4):
        # Set max size to tiny threshold (e.g. 0.0001 MB = 100 bytes)
        with pytest.raises(OversizedFileError, match="exceeds maximum allowed limit"):
            validate_video_file(synthetic_mp4, max_size_mb=0.0001)


class TestEndToEndDownload:
    """Integration test with a real public X video post."""

    def test_e2e_download_and_deduplication(self, tmp_path):
        test_url = "https://x.com/historyinmemes/status/1790637656616943991"

        # 1. First download (new)
        target_path, meta, is_new = download_x_video(
            post_url=test_url,
            downloads_dir=tmp_path,
            max_size_mb=DEFAULT_MAX_SIZE_MB,
        )

        assert is_new is True
        assert target_path.exists()
        assert target_path.name.startswith("1790637656616943991_")
        assert target_path.suffix == ".mp4"
        assert meta["duration"] > 10.0
        assert meta["width"] > 0
        assert meta["height"] > 0
        assert meta["size_bytes"] > 0

        # 2. Second download (duplicate check)
        target_path2, meta2, is_new2 = download_x_video(
            post_url=test_url,
            downloads_dir=tmp_path,
            max_size_mb=DEFAULT_MAX_SIZE_MB,
        )

        assert is_new2 is False
        assert target_path2 == target_path
        assert meta2["size_bytes"] == meta["size_bytes"]

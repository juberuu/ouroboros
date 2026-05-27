"""Tests for send_video file_path support and MIME detection."""
import base64
import pathlib
import types

from ouroboros.tools.core import _send_video, _detect_video_mime, _MAX_VIDEO_FILE_BYTES


def _make_ctx(chat_id=123):
    return types.SimpleNamespace(
        current_chat_id=chat_id,
        pending_events=[],
    )


class TestSendVideoFilePath:
    def test_file_path_reads_mp4(self, tmp_path):
        mp4 = tmp_path / "test.mp4"
        mp4.write_bytes(b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 100)

        ctx = _make_ctx()
        result = _send_video(ctx, file_path=str(mp4), caption="promo clip")

        assert "OK" in result
        assert len(ctx.pending_events) == 1
        assert ctx.pending_events[0]["type"] == "send_video"
        assert ctx.pending_events[0]["mime"] == "video/mp4"

    def test_file_not_found(self):
        ctx = _make_ctx()
        result = _send_video(ctx, file_path="/nonexistent/video.mp4")
        assert "not found" in result.lower()

    def test_file_too_large(self, tmp_path):
        big = tmp_path / "huge.mp4"
        big.write_bytes(b'\x00\x00\x00\x18ftypmp42' + b'\x00' * (_MAX_VIDEO_FILE_BYTES + 1))

        ctx = _make_ctx()
        result = _send_video(ctx, file_path=str(big))
        assert "too large" in result.lower()

    def test_no_input_returns_error(self):
        ctx = _make_ctx()
        result = _send_video(ctx)
        assert "provide" in result.lower()


class TestDetectVideoMime:
    def test_mp4_extension(self):
        assert _detect_video_mime("movie.mp4", b"") == "video/mp4"

    def test_mp4_header(self):
        assert _detect_video_mime("movie.dat", b'\x00\x00\x00\x18ftypmp42\x00') == "video/mp4"

    def test_webm_header(self):
        assert _detect_video_mime("movie.dat", b'\x1a\x45\xdf\xa3\x00') == "video/webm"

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, UploadFile

import main


class AudioConverterTests(unittest.TestCase):
    def make_upload(self, filename: str, data: bytes, content_type: str) -> UploadFile:
        payload = tempfile.SpooledTemporaryFile()
        payload.write(data)
        payload.seek(0)
        return UploadFile(file=payload, filename=filename, headers={"content-type": content_type})

    def convert(self, filename: str, data: bytes, content_type: str, target: str = "mp3"):
        upload = self.make_upload(filename, data, content_type)
        return asyncio.run(main.convert_audio(file=upload, target=target))

    def assert_http_error(self, status_code: int, filename: str, data: bytes, content_type: str, target: str):
        with self.assertRaises(HTTPException) as caught:
            self.convert(filename, data, content_type, target)
        self.assertEqual(caught.exception.status_code, status_code)
        return str(caught.exception.detail)

    def test_converts_to_supported_formats_and_cleans_temp_files(self):
        media_types = {
            "mp3": "audio/mpeg",
            "wav": "audio/wav",
            "ogg": "audio/ogg",
            "flac": "audio/flac",
            "aac": "audio/aac",
        }
        temp_dirs = []

        def fake_convert(input_path: Path, output_path: Path, target: str) -> None:
            self.assertTrue(input_path.exists())
            temp_dirs.append(input_path.parent)
            output_path.write_bytes(f"converted-{target}".encode("utf-8"))

        with patch.object(main, "_convert_audio_file", side_effect=fake_convert):
            for target, media_type in media_types.items():
                with self.subTest(target=target):
                    response = self.convert("song.wav", b"fake audio", "audio/wav", target)

                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.body, f"converted-{target}".encode("utf-8"))
                    self.assertEqual(response.media_type, media_type)
                    self.assertEqual(response.headers["x-audio-output-format"], target)
                    self.assertEqual(response.headers["x-audio-output-bytes"], str(len(response.body)))
                    self.assertEqual(
                        response.headers["content-disposition"],
                        f'attachment; filename="song.{target}"',
                    )

        self.assertTrue(temp_dirs)
        self.assertTrue(all(not directory.exists() for directory in temp_dirs))

    def test_rejects_unsupported_target(self):
        with patch.object(main, "_convert_audio_file") as converter:
            error = self.assert_http_error(415, "song.wav", b"fake audio", "audio/wav", "wma")

        self.assertIn("mp3, wav, ogg, flac, or aac", error)
        converter.assert_not_called()

    def test_rejects_non_audio_uploads(self):
        with patch.object(main, "_convert_audio_file") as converter:
            error = self.assert_http_error(415, "notes.txt", b"hello", "text/plain", "mp3")

        self.assertIn("Upload an audio file", error)
        converter.assert_not_called()

    def test_rejects_empty_audio_upload(self):
        with patch.object(main, "_convert_audio_file") as converter:
            error = self.assert_http_error(400, "song.wav", b"", "audio/wav", "mp3")

        self.assertIn("empty", error)
        converter.assert_not_called()

    def test_reports_ffmpeg_missing_as_unavailable(self):
        with patch.object(main, "_convert_audio_file", side_effect=RuntimeError("Audio conversion is not installed on the server.")):
            error = self.assert_http_error(503, "song.wav", b"fake audio", "audio/wav", "mp3")

        self.assertIn("not installed", error)

    def test_reports_conversion_failure_as_bad_upload(self):
        with patch.object(main, "_convert_audio_file", side_effect=ValueError("Invalid data found when processing input")):
            error = self.assert_http_error(400, "song.wav", b"fake audio", "audio/wav", "mp3")

        self.assertEqual(error, "Invalid data found when processing input")


if __name__ == "__main__":
    unittest.main()

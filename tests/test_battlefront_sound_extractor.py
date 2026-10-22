import importlib.util
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "battlefront_sound_extractor.py"
SPEC = importlib.util.spec_from_file_location("battlefront_sound_extractor", SCRIPT)
extractor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(extractor)


class ExtractorTests(unittest.TestCase):
    def test_source_uses_english_interface_strings(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("Browse...", source)
        self.assertIn("Input file (.lvl / .bnk):", source)
        self.assertNotIn("_TRANSLATIONS", source)
        self.assertNotIn("def tr(", source)

    def test_default_output_directory_is_next_to_source_and_uses_stem(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "sample_level.lvl"
            source.write_bytes(b"test")
            args = type("Args", (), {"outdir": None, "raw": False,
                                     "format": "auto", "blocksize": 2048})()
            expected = str(Path(temp) / "sample_level")
            with patch.object(extractor, "scan_extract", return_value=(1, 1)) as scan:
                result = extractor.process_file(str(source), args, {})
            self.assertEqual(result, 1)
            self.assertEqual(scan.call_args.args[2], expected)

    def test_wav_writer_creates_valid_pcm16_wav(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "test.wav"
            extractor.write_wav(str(path), [[0, 100, -100]], 22050)
            with extractor.wave.open(str(path), "rb") as wav:
                self.assertEqual(wav.getnchannels(), 1)
                self.assertEqual(wav.getsampwidth(), 2)
                self.assertEqual(wav.getframerate(), 22050)
                self.assertEqual(wav.getnframes(), 3)
                self.assertEqual(wav.readframes(3), struct.pack("<hhh", 0, 100, -100))

    def test_adpcm_decoder_handles_empty_data(self):
        self.assertEqual(extractor.decode_ima_adpcm_channels(b"", 1), [[]])


if __name__ == "__main__":
    unittest.main(verbosity=2)

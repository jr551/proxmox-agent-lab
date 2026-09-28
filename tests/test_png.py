"""Tests for `png.ppm_to_png`: QEMU screendump PPM blobs through our writer.

Every fixture is built inline as bytes -- no binary files, no network. The
happy paths assert the observable contract: the converted PNG decodes back
to the same RGB pixels with this module's own reader, and the bytes are
exactly what `encode_png` produces for that buffer (proving the conversion
goes through the existing writer, not around it). Every bad input must raise
`ValueError` with the reason -- never a crash, never a silently partial image.
"""

from __future__ import annotations

from pathlib import Path
import sys  # noqa: E402

# Shared bootstrap: fixture configuration plus a per-process state directory,
# applied before any proxmox_agent_lab import. `support` sits beside this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import unittest  # noqa: E402

from proxmox_agent_lab import png  # noqa: E402

PIXELS_2X2 = bytes(
    [
        255, 0, 0,    0, 255, 0,
        0, 0, 255,    255, 255, 255,
    ]
)
PIXELS_3X1 = bytes([0, 0, 0, 10, 20, 30, 255, 128, 64])


class PpmToPngTests(unittest.TestCase):
    def convert(self, data: bytes) -> tuple[int, int, bytes]:
        """Convert and decode with the module's own reader."""
        converted = png.ppm_to_png(data)
        self.assertEqual(converted[:8], b"\x89PNG\r\n\x1a\n")
        return png.decode_png(converted)

    # -- happy paths ------------------------------------------------------

    def test_happy_path_2x2_round_trips(self):
        blob = b"P6\n2 2\n255\n" + PIXELS_2X2
        width, height, rgb = self.convert(blob)
        self.assertEqual((width, height), (2, 2))
        self.assertEqual(rgb, PIXELS_2X2)
        # The bytes are exactly what the existing writer produces for this
        # buffer: the conversion goes through encode_png, not around it.
        self.assertEqual(png.ppm_to_png(blob), png.encode_png(2, 2, PIXELS_2X2))

    def test_happy_path_3x1_round_trips(self):
        blob = b"P6\n3 1\n255\n" + PIXELS_3X1
        width, height, rgb = self.convert(blob)
        self.assertEqual((width, height), (3, 1))
        self.assertEqual(rgb, PIXELS_3X1)

    def test_header_comments_and_whitespace_variants_are_accepted(self):
        # Comments after the magic, extra spaces/tabs, and newlines between
        # every field -- exactly what the format allows and what hand-built
        # or tool-built PPMs exercise.
        blob = (
            b"P6"
            b"\n# produced by a screendump\n"
            b"  3\t1 \n"
            b"# maxval follows\n"
            b"255\n"
            + PIXELS_3X1
        )
        width, height, rgb = self.convert(blob)
        self.assertEqual((width, height), (3, 1))
        self.assertEqual(rgb, PIXELS_3X1)

    # -- payload validation -----------------------------------------------

    def test_truncated_payload_raises(self):
        blob = b"P6\n2 2\n255\n" + PIXELS_2X2[:6]  # needs 12
        with self.assertRaisesRegex(ValueError, "length mismatch"):
            png.ppm_to_png(blob)

    def test_oversized_payload_raises(self):
        blob = b"P6\n2 2\n255\n" + PIXELS_2X2 + b"\x00"  # 13, needs 12
        with self.assertRaisesRegex(ValueError, "length mismatch"):
            png.ppm_to_png(blob)

    # -- header validation -------------------------------------------------

    def test_wrong_magic_raises(self):
        cases = {
            "P3": (b"P3\n2 2\n255\n" + PIXELS_2X2, "ASCII"),
            "P5": (b"P5\n2 2\n255\n" + PIXELS_2X2[:12], "grayscale"),
            "garbage": (b"NOTAPPM\nwhatever", "P6 magic"),
        }
        for name, (blob, expected) in cases.items():
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, expected):
                    png.ppm_to_png(blob)

    def test_non_255_maxval_rejected(self):
        # 16-bit (65535) and a low maxval (16) are both refused with the
        # reason in the message; neither may be silently rescaled.
        for maxval, expected in ((b"65535", "16-bit"), (b"16", "maxval 16")):
            with self.subTest(maxval=maxval):
                blob = b"P6\n2 2\n" + maxval + b"\n" + PIXELS_2X2
                with self.assertRaisesRegex(ValueError, expected):
                    png.ppm_to_png(blob)

    def test_zero_dimensions_rejected(self):
        # Zero must not sail through as an empty-but-valid-looking PNG.
        with self.assertRaisesRegex(ValueError, "no pixels"):
            png.ppm_to_png(b"P6\n0 5\n255\n")

    def test_header_ending_without_raster_raises(self):
        with self.assertRaises(ValueError):
            png.ppm_to_png(b"P6\n2 2\n255")  # no whitespace, no raster
        with self.assertRaises(ValueError):
            png.ppm_to_png(b"P6\n2 2\n")  # stops before maxval


if __name__ == "__main__":
    unittest.main()

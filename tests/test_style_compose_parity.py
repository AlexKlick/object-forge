from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path("/home/alexk/debt-city-greybox-spike/apps/greybox/assets/tools/style_compose.py")


class StyleComposeParityTests(unittest.TestCase):
    @unittest.skipUnless(SOURCE.is_file(), "repo B style_compose.py is absent; parity requires the spike checkout")
    def test_vendored_source_is_byte_identical_below_header(self):
        twin = (ROOT / "src/open_sprite_pipeline/style_compose.py").read_bytes()
        header, instruction, body = twin.split(b"\n", 2)
        self.assertEqual(header, b"# Source: " + str(SOURCE).encode())
        self.assertEqual(instruction, b"# keep byte-identical below this line")
        self.assertEqual(body, SOURCE.read_bytes())

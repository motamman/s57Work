"""enc-sources.yaml gap-fill groups: both schemas, and the repo's own file."""
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT, pipeline

s57 = pipeline()
try:
    import yaml  # noqa: F401
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False


@unittest.skipUnless(HAVE_YAML, "pyyaml not installed")
class GapFillConfig(unittest.TestCase):
    def write(self, text):
        p = Path(tempfile.mkdtemp()) / "enc-sources.yaml"
        p.write_text(text)
        return p

    def test_list_schema(self):
        groups = s57.load_gap_fill_config(self.write(
            "gap_fills:\n"
            "  - name: a\n    zoom_range: [9, 16]\n    cells: [US5RI1AC, US5RI1AD]\n"
            "    description: d\n"
            "  - name: empty\n    cells: []\n"
            "  - zoom_range: [11, 12]\n    cells: [US4NY1BY]\n"))
        self.assertEqual([g.name for g in groups], ["a", "gapfill2"])
        self.assertEqual(groups[0].zoom_range, (9, 16))
        self.assertEqual(groups[0].cells, ["US5RI1AC", "US5RI1AD"])
        self.assertEqual(groups[1].zoom_range, (11, 12))

    def test_legacy_dict_schema(self):
        groups = s57.load_gap_fill_config(self.write(
            "gap_fills:\n  zoom_range: [9, 10]\n  cells: [US5RI1AC]\n"))
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].name, "gapfill")

    def test_missing_section_or_file(self):
        self.assertEqual(s57.load_gap_fill_config(self.write("active: [x]\n")), [])
        self.assertEqual(s57.load_gap_fill_config(Path("/nonexistent.yaml")), [])

    def test_repo_config_parses(self):
        groups = s57.load_gap_fill_config(ROOT / "enc-sources.yaml")
        self.assertGreater(len(groups), 0)
        for g in groups:
            self.assertLessEqual(g.zoom_range[0], g.zoom_range[1])
            self.assertTrue(all(s57.ENC_CELL_RE.match(c) for c in g.cells), g.name)


if __name__ == "__main__":
    unittest.main()

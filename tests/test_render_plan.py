"""The finer-wins render plan: which zooms each source is tiled at, and
whose footprints are erased from it first. These expectations are the
runs the real 01CGD build produces (band + gap fill), so a change here
changes what ships."""
import unittest
from pathlib import Path

from tests.helpers import pipeline

s57 = pipeline()


def source(label, priority, zmin, zmax, native=None):
    return s57.RenderSource(priority=priority, label=label,
                            merged_dir=Path(f"/m/{label}"), footprint_files=[],
                            zoom_range=(zmin, zmax), layer_rules={},
                            native_zoom=native)


def plan():
    return [source("band2", 2, 9, 12, (9, 10)),
            source("band3", 3, 11, 14, (11, 12)),
            source("band4", 4, 13, 16, (13, 14)),
            source("band5", 5, 15, 16, (15, 16)),
            source("fill", 4.9, 9, 16)]


def runs_by_label(runs):
    out = {}
    for r in runs:
        out.setdefault(r.source.label, []).append(
            (r.zoom_range, sorted(e.label for e in r.erased_by)))
    return out


class PriorityAt(unittest.TestCase):
    def test_native_band_outranks_everything(self):
        band2 = source("band2", 2, 9, 12, (9, 10))
        fill = source("fill", 4.9, 9, 16)
        self.assertGreater(s57.priority_at(band2, 9), s57.priority_at(fill, 9))
        self.assertLess(s57.priority_at(band2, 11), s57.priority_at(fill, 11))

    def test_fill_beats_coarser_bands_loses_to_its_own_band(self):
        band4 = source("band4", 4, 13, 16, (13, 14))
        fill = source("fill", 4.9, 9, 16)
        self.assertLess(s57.priority_at(fill, 13), s57.priority_at(band4, 13))
        self.assertGreater(s57.priority_at(fill, 15), s57.priority_at(band4, 15))


class PlanRenderRuns(unittest.TestCase):
    def test_runs_match_the_district_plan(self):
        got = runs_by_label(s57.plan_render_runs(plan()))
        self.assertEqual(got["band2"], [((9, 10), []),
                                        ((11, 12), ["band3", "fill"])])
        self.assertEqual(got["band3"], [((11, 12), []),
                                        ((13, 14), ["band4", "fill"])])
        self.assertEqual(got["band4"], [((13, 14), []),
                                        ((15, 16), ["band5", "fill"])])
        self.assertEqual(got["band5"], [((15, 16), [])])
        self.assertEqual(got["fill"], [((9, 10), ["band2"]),
                                       ((11, 12), ["band3"]),
                                       ((13, 14), ["band4"]),
                                       ((15, 16), ["band5"])])

    def test_native_zooms_are_never_erased(self):
        for r in s57.plan_render_runs(plan()):
            nz = r.source.native_zoom
            if nz and nz[0] <= r.zoom_range[0] <= nz[1]:
                self.assertEqual(r.erased_by, [], r.stem)

    def test_runs_sorted_coarse_to_fine(self):
        runs = s57.plan_render_runs(plan())
        keys = [(r.source.priority, r.zoom_range[0]) for r in runs]
        self.assertEqual(keys, sorted(keys))

    def test_stem_names_zoom_subrange_and_erasers(self):
        stems = {r.stem for r in s57.plan_render_runs(plan())}
        # A source tiled in one piece keeps its bare label; a sub-range
        # names its zooms, and an erased run names its erasers.
        self.assertIn("band5", stems)
        self.assertIn("band4_z13-14", stems)
        self.assertIn("band4_z15-16.minus-band5+fill", stems)
        self.assertIn("fill_z9-10.minus-band2", stems)
        self.assertNotIn("band4", stems)

    def test_single_source_is_one_untouched_run(self):
        runs = s57.plan_render_runs([source("band5", 5, 15, 16, (15, 16))])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].zoom_range, (15, 16))
        self.assertEqual(runs[0].erased_by, [])
        self.assertEqual(runs[0].stem, "band5")


if __name__ == "__main__":
    unittest.main()

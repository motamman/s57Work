"""Consolidation: per-cell GeoJSON grouped into one file per layer, with
the per-feature zoom stamps applied, source identity recorded so an
override (Stage 2b clipped copy) appearing or emptying is noticed, and
fresh layers left alone on a rerun."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from tests.helpers import pipeline

s57 = pipeline()


def fc(*feats):
    return {"type": "FeatureCollection", "features": list(feats)}


def feat(lnam, geom_type="Point", coords=(-71.6, 41.05), **props):
    return {"type": "Feature", "properties": {"LNAM": lnam, "PRIM": 1, **props},
            "geometry": {"type": geom_type, "coordinates": list(coords)}}


def write(path, obj, pad=True):
    text = json.dumps(obj)
    if pad and len(text) <= 100:
        obj = dict(obj, note="x" * 120)
        text = json.dumps(obj)
    path.write_text(text)


class Consolidate(unittest.TestCase):
    def setUp(self):
        self.gj = Path(tempfile.mkdtemp()) / "band5"
        self.gj.mkdir()
        self.merged = self.gj.parent / "merged"
        write(self.gj / "DEPARE_US5RI1AC.geojson", fc(feat("D1", "Polygon", [[[0, 0], [1, 0], [1, 1], [0, 0]]], PRIM=3)))
        write(self.gj / "DEPARE_US5RI1AD.geojson", fc(feat("D2", "Polygon", [[[1, 0], [2, 0], [2, 1], [1, 0]]], PRIM=3)))
        write(self.gj / "M_COVR_US5RI1AC.geojson", fc(feat("C1", "Polygon", [[[0, 0], [1, 0], [1, 1], [0, 0]]], PRIM=3, CATCOV=1)))
        write(self.gj / "SOUNDG_US5RI1AC.geojson",
              fc(*[feat(f"S{i:03d}", coords=(-71.6 + i / 1000, 41.05)) for i in range(300)]))
        (self.gj / "EMPTY_US5RI1AC.geojson").write_text("{}")  # <= 100 bytes: ignored

    def load(self, name):
        return json.loads((self.merged / f"{name}.geojson").read_text())

    def test_groups_by_layer_and_stamps_soundings(self):
        rules = s57.layer_zoom_rules(5, 15)
        out = s57.consolidate_geojson(self.gj, self.merged, layer_rules=rules)
        self.assertEqual(sorted(p.stem for p in out), ["DEPARE", "M_COVR", "SOUNDG"])
        self.assertEqual(len(self.load("DEPARE")["features"]), 2)
        self.assertEqual(len(self.load("M_COVR")["features"]), 1)
        self.assertNotIn("tippecanoe", self.load("DEPARE")["features"][0])
        stamps = [f["tippecanoe"]["minzoom"] for f in self.load("SOUNDG")["features"]]
        self.assertEqual(set(stamps), {15, 16})
        self.assertAlmostEqual(stamps.count(15) / len(stamps), 0.4, delta=0.1)

    def test_rerun_leaves_fresh_layers_untouched(self):
        rules = s57.layer_zoom_rules(5, 15)
        s57.consolidate_geojson(self.gj, self.merged, layer_rules=rules)
        before = {p.name: p.stat().st_mtime_ns for p in self.merged.glob("*.geojson")}
        time.sleep(0.02)
        s57.consolidate_geojson(self.gj, self.merged, layer_rules=rules)
        after = {p.name: p.stat().st_mtime_ns for p in self.merged.glob("*.geojson")}
        self.assertEqual(before, after)

    def test_rule_change_rewrites_everything(self):
        s57.consolidate_geojson(self.gj, self.merged, layer_rules=s57.layer_zoom_rules(5, 15))
        time.sleep(0.02)
        s57.consolidate_geojson(self.gj, self.merged, layer_rules={"SOUNDG": s57.ZoomRule(16)})
        stamps = {f["tippecanoe"]["minzoom"] for f in self.load("SOUNDG")["features"]}
        self.assertEqual(stamps, {16})

    def test_override_replaces_a_cell_and_empty_override_drops_it(self):
        clipped = self.gj.parent / "band5.resolved"
        clipped.mkdir()
        write(clipped / "DEPARE_US5RI1AD.geojson", fc(feat("D2clip", "Polygon", [[[1, 0], [2, 0], [2, 0.5], [1, 0]]], PRIM=3)))
        ov = {"DEPARE_US5RI1AD.geojson": clipped / "DEPARE_US5RI1AD.geojson"}
        s57.consolidate_geojson(self.gj, self.merged, overrides=ov)
        names = [f["properties"]["LNAM"] for f in self.load("DEPARE")["features"]]
        self.assertEqual(sorted(names), ["D1", "D2clip"])
        # The override empties (cell fully under a reschemed neighbour):
        # its features vanish even though the plain export is older.
        write(clipped / "DEPARE_US5RI1AD.geojson", fc())
        s57.consolidate_geojson(self.gj, self.merged, overrides=ov)
        names = [f["properties"]["LNAM"] for f in self.load("DEPARE")["features"]]
        self.assertEqual(names, ["D1"])
        # Override removed again: the plain export is back.
        s57.consolidate_geojson(self.gj, self.merged)
        names = [f["properties"]["LNAM"] for f in self.load("DEPARE")["features"]]
        self.assertEqual(sorted(names), ["D1", "D2"])

    def test_orphan_merged_layer_is_removed(self):
        self.merged.mkdir(parents=True)
        (self.merged / "M.geojson").write_text("{}" * 60)
        s57.consolidate_geojson(self.gj, self.merged)
        self.assertFalse((self.merged / "M.geojson").exists())

    def test_empty_dir_yields_nothing(self):
        empty = self.gj.parent / "none"
        empty.mkdir()
        self.assertEqual(s57.consolidate_geojson(empty, self.merged), [])


class GeojsonHasFeatures(unittest.TestCase):
    def test_padded_empty_collection_is_detected(self):
        p = Path(tempfile.mkdtemp()) / "x.geojson"
        p.write_text(json.dumps({"type": "FeatureCollection", "note": "x" * 200, "features": []}))
        self.assertFalse(s57._geojson_has_features(p))
        p.write_text(json.dumps(fc(feat("A"))))
        self.assertTrue(s57._geojson_has_features(p))


if __name__ == "__main__":
    unittest.main()

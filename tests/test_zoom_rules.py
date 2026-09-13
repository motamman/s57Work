"""Per-feature zoom presence (soundg_rule / feature_minzoom). This is the
rule that put SOUNDG at every zoom again on 2026-09-07; the numbers here
are the contract: bands 1-2 at their top zoom only, band 3+ from the
native bottom zoom with one in 2.5 kept, and a pick that survives the
GDAL round trip of the erase."""
import json
import random
import unittest

from tests.helpers import pipeline

s57 = pipeline()


def point(lnam, lon, lat, depth=1.0):
    return {"type": "Feature",
            "properties": {"LNAM": lnam, "DEPTH": depth},
            "geometry": {"type": "Point", "coordinates": [lon, lat, depth]}}


class SoundgRule(unittest.TestCase):
    def test_overview_bands_top_zoom_only(self):
        self.assertEqual(s57.soundg_rule(1, 7), s57.ZoomRule(8))
        self.assertEqual(s57.soundg_rule(2, 9), s57.ZoomRule(10))

    def test_detail_bands_from_native_bottom_thinned(self):
        for band, bottom in ((3, 11), (4, 13), (5, 15), (6, 17)):
            rule = s57.soundg_rule(band, bottom)
            self.assertEqual(rule.minzoom, bottom)
            self.assertEqual(rule.thin_rate, s57.SOUNDG_THIN_RATE)

    def test_gap_fill_never_below_z10(self):
        self.assertEqual(s57.soundg_rule(5, 9).minzoom, s57.SOUNDG_MIN_ZOOM)
        self.assertEqual(s57.soundg_rule(4, 11).minzoom, 11)

    def test_layer_rules_only_gate_soundg(self):
        self.assertEqual(set(s57.layer_zoom_rules(4, 13)), {"SOUNDG"})

    def test_marker_serialisation_changes_with_rule(self):
        a = s57.ZoomRule(13, 2.5).as_json()
        b = s57.ZoomRule(14).as_json()
        self.assertNotEqual(json.dumps(a), json.dumps(b))


class FeatureMinzoom(unittest.TestCase):
    def setUp(self):
        rng = random.Random(7)
        self.feats = [point(f"{i:016X}", -71 + rng.random(), 41 + rng.random())
                      for i in range(4000)]
        self.rule = s57.soundg_rule(4, 13)

    def test_share_at_bottom_zoom_is_one_in_rate(self):
        at_bottom = sum(1 for f in self.feats
                        if s57.feature_minzoom(f, self.rule) == 13)
        share = at_bottom / len(self.feats)
        self.assertAlmostEqual(share, 1 / s57.SOUNDG_THIN_RATE, delta=0.03)

    def test_other_features_go_one_zoom_up(self):
        zooms = {s57.feature_minzoom(f, self.rule) for f in self.feats}
        self.assertEqual(zooms, {13, 14})

    def test_pick_is_deterministic_and_position_based(self):
        f = self.feats[0]
        first = s57.feature_minzoom(f, self.rule)
        self.assertEqual(s57.feature_minzoom(f, self.rule), first)
        # Same LNAM, other position (a sounding split from a MultiPoint)
        # is an independent pick; over many, both zooms occur.
        picks = {s57.feature_minzoom(point(f["properties"]["LNAM"], -70 + i / 97, 40),
                                     self.rule) for i in range(200)}
        self.assertEqual(picks, {13, 14})

    def test_pick_survives_the_erase_round_trip(self):
        # GDAL writes GeoJSON coordinates with 7 decimals (RFC 7946) and
        # trims trailing zeros; the erase's ogr2ogr pass re-serialises
        # them the same way and drops the Z. The pick must not change.
        for f in self.feats[:500]:
            lon, lat, z = f["geometry"]["coordinates"]
            f["geometry"]["coordinates"] = [round(lon, 7), round(lat, 7), z]
            g = json.loads(json.dumps(f))
            g["geometry"]["coordinates"] = [float(f"{round(lon, 7):.7f}"),
                                            float(f"{round(lat, 7):.7f}")]
            self.assertEqual(s57.feature_minzoom(f, self.rule),
                             s57.feature_minzoom(g, self.rule))

    def test_unthinned_rule_is_constant(self):
        rule = s57.ZoomRule(10)
        self.assertTrue(all(s57.feature_minzoom(f, rule) == 10
                            for f in self.feats[:100]))

    def test_feature_without_geometry_does_not_crash(self):
        f = {"type": "Feature", "properties": {"LNAM": "X"}, "geometry": None}
        self.assertIn(s57.feature_minzoom(f, self.rule), (13, 14))


class StampFeature(unittest.TestCase):
    def test_stamp_sets_tippecanoe_extension(self):
        f = point("A", -71.5, 41.2)
        s57.stamp_feature(f, s57.ZoomRule(12))
        self.assertEqual(f["tippecanoe"], {"minzoom": 12})

    def test_stamp_keeps_other_extension_keys(self):
        f = point("A", -71.5, 41.2)
        f["tippecanoe"] = {"maxzoom": 14}
        s57.stamp_feature(f, s57.ZoomRule(12))
        self.assertEqual(f["tippecanoe"], {"maxzoom": 14, "minzoom": 12})

    def test_no_rule_leaves_feature_alone(self):
        f = point("A", -71.5, 41.2)
        s57.stamp_feature(f, None)
        self.assertNotIn("tippecanoe", f)


if __name__ == "__main__":
    unittest.main()

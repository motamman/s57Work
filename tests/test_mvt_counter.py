"""count-layer-by-zoom.py's in-process Mapbox Vector Tile reader, checked
against tiles encoded by hand, so the verification tool itself is
verified without tippecanoe."""
import gzip
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.helpers import counter

c = counter()


def varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def field(num, payload):
    return varint((num << 3) | 2) + varint(len(payload)) + payload


def layer(name, nfeatures, extra=b""):
    body = field(1, name.encode())
    body += b"".join(field(2, b"\x08\x01") for _ in range(nfeatures))  # Feature{id=1}
    body += varint((15 << 3) | 0) + varint(2)                          # version
    return field(3, body + extra)


class LayerFeatureCounts(unittest.TestCase):
    def test_counts_features_per_layer(self):
        tile = layer("SOUNDG", 3) + layer("DEPARE", 1) + layer("SOUNDG", 2)
        got = c.layer_feature_counts(gzip.compress(tile))
        self.assertEqual(got["SOUNDG"][0], 5)
        self.assertEqual(got["DEPARE"][0], 1)

    def test_plain_and_zlib_tiles(self):
        import zlib
        tile = layer("LIGHTS", 4)
        self.assertEqual(c.layer_feature_counts(tile)["LIGHTS"][0], 4)
        self.assertEqual(c.layer_feature_counts(zlib.compress(tile))["LIGHTS"][0], 4)

    def test_unknown_fields_are_skipped(self):
        extra = varint((9 << 3) | 0) + varint(300)          # varint field
        extra += varint((10 << 3) | 5) + b"\x00\x00\x00\x00"  # 32-bit field
        extra += varint((11 << 3) | 1) + b"\x00" * 8          # 64-bit field
        tile = layer("WRECKS", 2, extra)
        self.assertEqual(c.layer_feature_counts(tile)["WRECKS"][0], 2)

    def test_layer_bytes_are_the_layer_message_size(self):
        tile = layer("SOUNDG", 3)
        n, nbytes = c.layer_feature_counts(tile)["SOUNDG"]
        self.assertEqual(nbytes, len(tile) - 2)  # minus tag + length prefix


class TilesInBbox(unittest.TestCase):
    def test_tms_flip_and_bbox_selection(self):
        z = 13
        x, y = c.lonlat_to_tile(-71.58, 41.17, z)
        p = Path(tempfile.mkdtemp()) / "t.mbtiles"
        db = sqlite3.connect(str(p))
        db.execute("CREATE TABLE tiles (zoom_level int, tile_column int, tile_row int, tile_data blob)")
        top = (1 << z) - 1
        db.execute("INSERT INTO tiles VALUES (?,?,?,?)", (z, x, top - y, gzip.compress(layer("SOUNDG", 7))))
        db.execute("INSERT INTO tiles VALUES (?,?,?,?)", (z, x + 50, top - y, gzip.compress(layer("SOUNDG", 1))))
        db.commit()
        got = list(c.tiles_in_bbox(db, z, (-71.6, 41.1, -71.5, 41.2)))
        self.assertEqual([(gx, gy) for gx, gy, _ in got], [(x, y)])
        res = c.measure(p, ["SOUNDG", "DEPARE"], (-71.6, 41.1, -71.5, 41.2), [z, 14])
        self.assertEqual(res["SOUNDG"][z]["features"], 7)
        self.assertEqual(res["SOUNDG"][z]["max_per_tile"], 7)
        self.assertEqual(res["DEPARE"][z]["tiles_with_layer"], 0)
        self.assertEqual(res["SOUNDG"][14]["tiles"], 0)

    def test_pole_latitude_does_not_raise(self):
        self.assertEqual(c.lonlat_to_tile(0, -90, 3)[1], 7)


if __name__ == "__main__":
    unittest.main()

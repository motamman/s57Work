# Depth Soundings on S-57 Vector Charts: Problem and Fix

## The Problem

After converting NOAA S-57 ENC charts to vector MBTiles using `s57-to-mbtiles.py` and serving them through SignalK's `signalk-charts-provider-simple`, depth soundings (the numbers on nautical charts showing water depth) were completely absent from the rendered maps in Freeboard-SK.

This turned out to be **two separate bugs** — one in the tile generation pipeline, and one in the Freeboard-SK web application.

---

## Bug 1: Missing Depth Values in Vector Tiles

### Root Cause

S-57 ENC charts store depth soundings (the `SOUNDG` layer) as **MultiPointZ** geometry. Each feature is a single MultiPoint containing hundreds of individual soundings, with the depth encoded as the **Z coordinate** of each point (`[longitude, latitude, depth]`).

When `ogr2ogr` exports this to GeoJSON, two things go wrong:

1. **MultiPoint stays as MultiPoint** — a single feature with hundreds of points. Vector tile renderers can't place individual labels on each sounding because they're all one geometry.

2. **Depth is only in the Z coordinate** — it's buried in the coordinates array `[-70.649, 42.550, 47.7]` but not in the feature properties. Vector tile renderers read **properties** for label text, not geometry coordinates.

The result: the SOUNDG points exist in the tiles but carry no usable depth value.

### Fix

GDAL's S-57 driver has two open options designed exactly for this:

- **`SPLIT_MULTIPOINT=YES`** — explodes each MultiPoint into individual Point features (one per sounding)
- **`ADD_SOUNDG_DEPTH=YES`** — copies the Z coordinate into a `DEPTH` property on each feature

In `s57-to-mbtiles.py`, the GDAL export shell script was updated to detect the SOUNDG layer and apply these options:

```bash
# Before (all layers treated the same):
ogr2ogr -f GeoJSON "/output/$outname.geojson" "$enc" "$layer" 2>/dev/null || true

# After (SOUNDG gets special handling):
if [ "$layer" = "SOUNDG" ]; then
  ogr2ogr -f GeoJSON -oo SPLIT_MULTIPOINT=YES -oo ADD_SOUNDG_DEPTH=YES \
    "/output/$outname.geojson" "$enc" "$layer" 2>/dev/null || true
else
  ogr2ogr -f GeoJSON "/output/$outname.geojson" "$enc" "$layer" 2>/dev/null || true
fi
```

After this fix, each sounding is an individual GeoJSON Point feature with a `DEPTH` property:

```json
{
  "type": "Feature",
  "properties": {
    "RCID": 1000,
    "DEPTH": 47.7,
    ...
  },
  "geometry": {
    "type": "Point",
    "coordinates": [-70.6497243, 42.5504785, 47.7]
  }
}
```

tippecanoe warns `ignoring dimensions beyond two` for the Z coordinate in the geometry — this is harmless because the depth is now in the `DEPTH` property where it belongs.

### Files Changed

- `~/s57-to-mbtiles.py` — added SOUNDG branch in the GDAL export script (lines 207-212)

---

## Bug 2: Freeboard-SK Doesn't Render SOUNDG

### Root Cause

Even with correct depth data in the vector tiles, Freeboard-SK displayed nothing.

Freeboard-SK's S-57 renderer (`s57Style.ts`) implements the S-52 Presentation Library for chart symbology. It uses a `chartsymbols.xml` file that defines how each S-57 object class should be rendered. The XML includes entries for SOUNDG:

```xml
<lookup id="1259" RCID="31311" name="SOUNDG">
    <instruction>CS(SOUNDG02)</instruction>
```

`CS(SOUNDG02)` is a **Conditional Symbology** procedure — a function that reads feature properties and returns rendering instructions. The S-57 renderer has a `switch` statement in `evalCS()` that dispatches to the appropriate CS function:

```typescript
switch (instrParts[2]) {
    case 'LIGHTS05':
        retval = this.GetCSLIGHTS05(feature);
        break;
    case 'DEPCNT02':
        retval = this.GetCSDEPCNT02(feature);
        break;
    // ... other cases ...
    default:
        console.debug('Unsupported CS:' + instruction);
}
```

**SOUNDG02 was not implemented.** It fell through to the `default` case and was silently logged as "Unsupported CS". No rendering instructions were returned, so no soundings were drawn.

Only three depth-related CS procedures were implemented: `DEPARE01/02` (depth areas), `DEPCNT02` (depth contours), and `LIGHTS05` (lights). None of the others in the compiled JS bundle referenced SOUNDG at all.

### Fix

Four changes to `s57Style.ts`:

**1. Implement GetCSSOUNDG02 (~35 lines)**

The method reads the depth value from either `DEPTH` (our tiles via `ADD_SOUNDG_DEPTH`) or `VALSOU` (native S-57), splits it into whole meters and tenths, stores them as synthetic properties on the feature, and returns TX (text) rendering instructions:

```typescript
private GetCSSOUNDG02(feature: Feature): string[] {
    const retval: string[] = [];
    const featureProperties = feature.getProperties();

    let depth = NaN;
    if (featureProperties['DEPTH'] !== undefined) {
        depth = parseFloat(featureProperties['DEPTH']);
    } else if (featureProperties['VALSOU'] !== undefined) {
        depth = parseFloat(featureProperties['VALSOU']);
    }

    if (isNaN(depth)) {
        return retval;
    }

    const isNegative = depth < 0;
    const absDepth = Math.abs(depth);
    const wholePart = Math.floor(absDepth);
    const decimalPart = Math.round((absDepth - wholePart) * 10);

    const wholeStr = (isNegative ? '-' : '') + wholePart.toString();
    featureProperties['_SOUNDG_WHOLE'] = wholeStr;

    // Whole meters - centered, above baseline
    retval.push('TX(_SOUNDG_WHOLE,1,1,2)');

    // Decimal (tenths) - centered, below baseline
    if (decimalPart > 0) {
        featureProperties['_SOUNDG_FRAC'] = decimalPart.toString();
        retval.push('TX(_SOUNDG_FRAC,1,3,2)');
    }

    return retval;
}
```

The TX instruction format is `TX(property_name, hjust, vjust, space)` where:
- `hjust=1` = center aligned
- `vjust=1` = bottom baseline (positions whole meters above center)
- `vjust=3` = top baseline (positions tenths below center)
- `space=2` = standard spacing

This produces the traditional nautical chart sounding format: whole meters on top, tenths below. For example, a depth of 4.2 meters renders as:
```
 4
 2
```

**2. Add SOUNDG02 to the evalCS switch (3 lines)**

```typescript
case 'SOUNDG02':
    retval = this.GetCSSOUNDG02(feature);
    break;
```

**3. Add SOUNDG to layer ordering (2 lines)**

```typescript
case 'SOUNDG':
    return 7;
```

This ensures soundings render above depth areas and contours but below navigation aids.

**4. Add SOUNDG to the feature filter (1 line)**

```typescript
lup.name === 'DEPCNT' ||
lup.name === 'SOUNDG'
```

The S-57 renderer has a display category filter that only renders `DISPLAYBASE`, `STANDARD`, and `MARINERS_STANDARD` features. SOUNDG's display category in the chartsymbols.xml is `OTHER` (not one of the three), so it was being filtered out even if the CS procedure existed. Adding it to the exception list (like `DEPCNT` before it) ensures it renders regardless of category.

### Key Pitfall: RenderFeature vs Feature

The initial implementation used `feature.set('_SOUNDG_WHOLE', wholeStr, true)` to store the computed text. This crashed with `TypeError: t.set is not a function` because vector tile features in OpenLayers are **RenderFeature** objects, not full **Feature** objects. RenderFeature is a lightweight read-optimized class that doesn't have `.set()`.

The fix: write directly to the properties object returned by `getProperties()`, which returns a mutable reference:

```typescript
// Crashes - RenderFeature has no .set()
feature.set('_SOUNDG_WHOLE', wholeStr, true);

// Works - getProperties() returns a mutable object
featureProperties['_SOUNDG_WHOLE'] = wholeStr;
```

### Files Changed

- `~/freeboard-sk/src/app/modules/map/ol/lib/charts/s57Style.ts` — all four changes above

---

## Bug 3: tile-join metadata table (minor)

When merging per-zoom mbtiles with `tile-join`, the output file sometimes lacks a `metadata` table (depending on tile-join version). The script's `_patch_metadata()` function assumed the table existed and failed with `sqlite3.OperationalError: no such table: metadata`.

### Fix

Added `CREATE TABLE IF NOT EXISTS` and `CREATE UNIQUE INDEX IF NOT EXISTS` before the INSERT statements:

```python
def _patch_metadata(mbtiles_path: Path, name: str):
    db = sqlite3.connect(str(mbtiles_path))
    db.execute("CREATE TABLE IF NOT EXISTS metadata (name text, value text)")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS name ON metadata (name)")
    db.execute("INSERT OR REPLACE INTO metadata (name, value) VALUES ('type', 'S-57')")
    db.execute("INSERT OR REPLACE INTO metadata (name, value) VALUES ('name', ?)", (name,))
    db.execute("INSERT OR REPLACE INTO metadata (name, value) VALUES ('description', ?)", (name,))
    db.commit()
    db.close()
```

### Files Changed

- `~/s57-to-mbtiles.py` — `_patch_metadata()` function (lines 303-311)

---

## Summary of All Changes

| File | Change | Purpose |
|------|--------|---------|
| `s57-to-mbtiles.py` | SOUNDG branch in GDAL export | Split MultiPoint + add DEPTH property |
| `s57-to-mbtiles.py` | CREATE TABLE IF NOT EXISTS in _patch_metadata | Handle missing metadata table from tile-join |
| `freeboard-sk/.../s57Style.ts` | GetCSSOUNDG02 method | Render depth as split whole/decimal text |
| `freeboard-sk/.../s57Style.ts` | SOUNDG02 case in evalCS | Route SOUNDG to the new method |
| `freeboard-sk/.../s57Style.ts` | SOUNDG in layerOrder | Correct render ordering |
| `freeboard-sk/.../s57Style.ts` | SOUNDG in feature filter | Allow SOUNDG past display category filter |

## Rebuilding After Changes

### Regenerate tiles (only needed once after s57-to-mbtiles.py fix):
```bash
nohup python3 ~/s57-to-mbtiles.py <ENC_DIR> --by-band \
  -o ct-ri-ma-ny-layers.mbtiles \
  --output-dir ~/.signalk/charts-simple/ \
  > ~/s57-rebuild.log 2>&1 &
```

### Rebuild and deploy Freeboard-SK (after s57Style.ts changes):
```bash
cd ~/freeboard-sk
npm install
npx ng build
rm -rf ~/.signalk/node_modules/@signalk/freeboard-sk/public
cp -r public ~/.signalk/node_modules/@signalk/freeboard-sk/public
```

Then hard-refresh the browser (Ctrl+Shift+R). No SignalK restart needed for frontend-only changes.

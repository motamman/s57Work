# Same-band cell overlap in NOAA ENCs (rescheming transition)

Status: root cause found 2026-09-05 (cancelled cells shipped in NOAA's zips,
see below); handled by dropping cancelled cells at inventory plus Stage 2b
for the one remaining live-versus-live overlap. See "Mechanism".

## The symptom

Tiles in the September 2026 district builds contain the same S-57 object twice:
same layer, same `LNAM` (feature object identifier), same geometry, different
`RCID` and often different `SCAMIN`. Measured with `check-tile-duplicates.py`
against the published R2 files:

| District | Where | Zooms | Example |
|---|---|---|---|
| 01 | Block Island, Boston approach | z9-10 | 27 duplicates in one z10 tile, two `M_COVR` cells |
| 05 | Delaware Bay, NY approach | z9-12 | 14 duplicates at z10 |
| 07 | Miami, Straits of Florida, San Juan | z11-12 | 9 duplicates at z12 |
| 09 | Chicago, Duluth | z9-12 | 51 duplicates at z10, one beacon with `RCID` 5 and 21, `SCAMIN` 2,999,999 and 1,499,999 |
| 11 | San Diego, Long Beach | z9-10, z13-14 | 37 duplicates at z10, five cells in one z9 tile |
| 14 | Honolulu, Kahului, Guam | z9-12 | 42 duplicates at z10 |
| 08 | none | | Gulf band 2 has not been reschemed yet |

This is distinct from the band-versus-band stacking fixed by the finer-wins
erase (Stage 3b). The erase only separates *different* bands. Here two cells of
the *same* band cover the same water, and the pipeline treats cells of one
band as a non-overlapping tiling.

## Cause: legacy and reschemed cells shipped together

NOAA is replacing its legacy, paper-chart-shaped ENC cells with a rectangular
grid of "reschemed" cells (NOAA ENC Design Handbook, June 2024). Reschemed
cell names carry a regional code in characters 4-6: `GLB` for band 1,
ocean/lake codes such as `ATL`, `PAC`, `GRL`, `ARC` for band 2, state codes
with a digit for bands 3-4, and UN/LOCODE port codes for bands 5-6, followed
by a two-letter matrix position (`US2ATLPD`, `US2PACTZ`, `US2GRLAB`,
`US3RI1AA`). Legacy cells look like `US2EC03M`, `US2MI01M`, `US3EC11M`.

NOAA's product catalog (`ENCProdCat_19115.xml`, fetched 2026-09-03) lists
both generations as active for the same water:

| Cell | Generation | Edition | Scale | Issued | Covers |
|---|---|---|---|---|---|
| US2ATLPD "Atlantic" | reschemed | 1.3 | 1:700,000 | 2026-08-28 | Block Island |
| US2EC03M "Cape Sable to Cape Hatteras" | legacy | 39.5 | 1:1,200,000 | 2026-06-09 | Block Island, Delaware Bay |
| US2ATLPF "Atlantic" | reschemed | 1.0 | 1:700,000 | 2026-07-16 | Boston approach |
| US2EC04M | legacy | 41.1 | 1:675,000 | 2026-07-24 | Boston approach |
| US2GRLAB.. US2GRLCC (7 cells) | reschemed | 1.0 | 1:700,000 | 2026-05-07 | Great Lakes |
| US2MI01M | legacy | 0, update 10 | 1:500,000 | 2026-05-07 | Chicago (south Lake Michigan) |
| US2MI60M, US2MI79M, US2MI80M | legacy | | | | Great Lakes |

The Great Lakes district zip that CI built on 2026-09-04 contained all eleven
band 2 cells above (from the CI job log's export lines), so the duplicates at
Chicago are the legacy `US2MI*` cells under the reschemed `US2GRL*` grid.

In the overlap both cells carry `M_COVR` objects with `CATCOV = 1` ("coverage
available"): the Block Island z10 tile holds two such polygons with different
`LNAM`s, and so does the Chicago z10 tile. Neither cell has been cut back.

Verified against the raw source, not our tiles (2026-09-04): inside NOAA's
own `09CGD_ENCs.zip`, the file CI built from, `ogrinfo` on `US2GRLAB.000`
(DSID: edition 1, 1:700,000) and `US2MI01M.000` (DSID: edition 0 update 10,
1:500,000) shows each cell's `CATCOV = 1` polygon containing Chicago, and the
same beacon `LNAM 022602021B320032` at the same coordinates in both files
(`RCID` 5, `SCAMIN` 2,999,999 in the reschemed cell; `RCID` 21, `SCAMIN`
1,499,999 in the legacy cell). The two `M_COVR` `LNAM`s in the raw files are
exactly the two seen in the Chicago tile. The pipeline reproduces the
overlap; it does not create it.

## The actual cause: cancelled cells are still in the district zips (2026-09-05)

Checked in the raw NOAA zips for all eight districts, not in our tiles.
Every legacy cell in an overlapping pair has been **cancelled by NOAA** in
the way S-57 prescribes. S-57 Appendix B.1 §5.7:

> "In order to delete a data set, an update cell file is created,
> containing only the Data Set General Information record with the 'Data
> Set Identifier' [DSID] field. The 'Edition Number' [EDTN] subfield must
> be set to 0. This message is only used to cancel a base cell file."

That is exactly what the last update file of each of these cells is: about
400 bytes, no feature layers, edition number 0. Examples from the zips:

| Cell | Base edition | Last update | Cancelled on |
|---|---|---|---|
| US3OR01M | 39 | .007, DSID only, EDTN 0 | 2026-04-09 |
| US5WA13M | 41 | .001, DSID only, EDTN 0 | 2026-05-18 |
| US2MI01M | 11 | .010, DSID only, EDTN 0 | 2026-05-07 |
| US2EC04M | 41 | cancelled (EDTN 0 after updates) | 2026 |

GDAL applies update files on open by default ("The S-57 reader will
normally read and apply all updates files to the in memory version of the
base file on the fly", `UPDATES=APPLY`), so `DSID_EDTN` reads 0 for these
cells, but the driver does not drop the features of a cancelled cell. An
ECDIS removes the cell; our pipeline kept rendering it.

Count across the eight district zips (5,114 cells):

| District | Cells | Cancelled | Flagged overlap pairs | Pairs with exactly one cancelled side |
|---|---|---|---|---|
| 01 | 874 | 6 | 34 | 31 |
| 05 | 914 | 47 | 61 | 60 |
| 07 | 1032 | 12 | 55 | 55 |
| 08 | 819 | 2 | 1 | 1 |
| 09 | 964 | 9 | 13 | 13 |
| 11 | 436 | 43 | 83 | 83 |
| 13 | 561 | 34 | 95 | 95 |
| 14 | 514 | 37 | 106 | 106 |

444 of 448 overlapping pairs are a cancelled cell against a live one,
including every band 3-5 pair and every odd-named Pacific-territory pair.
The four exceptions are one cell: **US2EC03M** "Cape Sable to Cape
Hatteras" (edition 39, live) overlapping the live reschemed cells
US2ATLPC, US2ATLPD, US2ATLPF and US2ATLQD. That is the only genuine
live-versus-live transition overlap in the data.

Consequences:

- The correct primary rule is the standard's: **a cell whose edition
  number is 0 after updates is cancelled and is excluded from the build.**
  No name patterns, no overlap heuristics; it applies to every band and
  self-retires as NOAA removes the cancelled files from its zips.
- Name-based classification would have been wrong in both directions:
  dozens of legacy-named cells are live (e.g. 24 Great Lakes band 5 cells
  at editions 4-7) and several cancelled cells have non-legacy names
  (US1EEZ1M, US2FAS01, US409860).
- The catalog's "edition" is the last live edition (US2EC04M shows 41.1
  there while its shipped data is cancelled), so the catalog cannot be
  used to detect cancellation; only the DSID after updates can.
- The Stage 2b overlap detection stays useful as a check for live-vs-live
  overlaps; after cancelled cells are dropped it should find exactly the
  US2EC03M pairs, which need a policy decision (reschemed cell wins, or
  leave both).

## What the standards say

**IHO S-57 Appendix B.1 (ENC Product Specification) clause 2.2**, as quoted in
IC-ENC Policy D10 (ICE-P1 v3, 2017):

> "Cells with the same navigational purpose may overlap. However, data within
> the cells must not overlap. Therefore, in the area of overlap only one cell
> may contain data, all other cells must have a meta object M_COVR with
> CATCOV = 2 covering the overlap area. This rule applies even if several
> producers are involved."

**IHB Circular Letter 47/2004**, same source:

> "There must be no overlapping data between cells of the same navigational
> purpose (see S-57, Appendix B.1 clause 2.2), except at national boundaries,
> where, if it is difficult to achieve a perfect join, a 5 metre overlapping
> buffer zone may be used."

On what display systems do when the rule is broken, the same policy says:

> "Research has also identified that overlapping data causes serious problems
> for users of certain ECDIS which display both overlapping cells. The
> navigator is then presented with different representations of the same
> area, and which may cause data consistency problems, most notably with
> inconsistent depth areas."

and, in its risk criteria: "some ECDIS will display the larger scale
automatically where overlaps occur", while noting "the underlying concern of
unpredictable behaviour of certain ECDIS with any type of overlap".

So the data NOAA is shipping is, by the IHO's own definition, an overlap
error, and there is no standard rule for which cell a display should choose.

## What NOAA says it intends

From "An Overview of the NOAA ENC Re-scheming Plan", Nyberg, Harmon, Pe'eri,
Catoire, *International Hydrographic Review*, 30 November 2020:

> "Large amounts of data must be applied to both current and new products
> while the new charts are being built; the old chart must be retired or cut
> to new extents, simultaneously with the new ENC release, or overlap errors
> will occur."

> "Additionally, when charts are retired by area (rather than individual cell)
> communication with the mariner is critical."

NOAA's stated intent is therefore that the reschemed cell supersedes the
legacy one at release. In the districts measured above that retirement or
cut-back has not happened yet in the distributed data.

## Consequences for this pipeline

1. Within one band, cells cannot be assumed disjoint. Consolidation
   concatenates every cell's features, so the overlap becomes duplicate
   features in the merged layer and in every tile that covers it.
2. The band-level erase does not help, because both cells are in the same
   band and get the same priority.
3. The duplicates are exact: the same object with the same `LNAM`. That makes
   the problem measurable (`check-tile-duplicates.py`) and, in principle,
   fixable by choosing one cell per overlap area.

## Choosing a winner

There is no rule in S-57 or S-52 for this, because the situation is not
supposed to exist. The candidates, with what the data says about each:

- **Newest issue date wins** (`DSID` field `ISDT`, or edition `EDTN`). Matches
  NOAA's stated intent that the new release supersedes. Picks `US2ATLPD`
  (2026-08-28) over `US2EC03M` (2026-06-09) at Block Island, but at Boston the
  legacy `US2EC04M` (2026-07-24) was re-issued *after* the reschemed
  `US2ATLPF` (2026-07-16), so a pure date rule picks the legacy cell there.
  Dates alone are not enough.
- **Larger compilation scale wins** (`DSID`/`DSPM_CSCL`). This is what "some
  ECDIS" do. Picks `US2ATLPD` (1:700,000) over `US2EC03M` (1:1,200,000), but
  picks legacy `US2EC04M` (1:675,000) over reschemed `US2ATLPF` (1:700,000),
  and legacy `US2MI01M` (1:500,000) over reschemed `US2GRLAB` (1:700,000) at
  Chicago. Also not enough on its own.
- **Reschemed cell wins**, recognised by NOAA's naming convention from the
  Design Handbook. Deterministic and matches NOAA's intent in every case, but
  it is a NOAA-specific heuristic: the region-code pattern is unambiguous for
  bands 1-2 (`GLB`, `ATL`, `PAC`, `GRL`, `ARC`) and overlaps with legacy naming
  in bands 3-5, where legacy state cells such as `US5MA1SK` use the same
  shape. No same-band overlaps were measured in bands 3-5 beyond cell-edge
  sharing, so in practice the rule is only exercised in bands 1-2 today.

Whatever rule is chosen, the mechanism is the same as Stage 3b: for each
cell, erase the union of the winning cells' `M_COVR` `CATCOV=1` footprints
from the losing cell's features before consolidation, so that in the overlap
only one cell contributes data, which is exactly what S-57 2.2 requires the
producer to have done.

## Mechanism (Stage 2b in `s57-to-mbtiles.py`)

Runs per band between the GDAL export and consolidation, in by-band mode.

0. **Drop cancelled cells first** (`drop_cancelled_cells`, at inventory,
   all modes). A cell whose DSID edition number is 0 after updates has
   been cancelled per S-57 App. B.1 §5.7 and is excluded from the build,
   listed in `data/cancelled-cells.json`. This alone removes 444 of the
   448 overlaps measured on 2026-09-05.
1. **Classify** the remaining live cells by name: `reschemed` when
   characters 4-6 are a Design Handbook Annex A region code for band 1
   (`GLB`) or band 2 (`ARC ANT ATL GRL GOM PAC`), else `other`. A pair is
   resolved only when exactly one side is `reschemed`; today that is the
   live legacy `US2EC03M` under four live `US2ATL*` cells. Everything else
   (both other, both reschemed) is warned and recorded, never erased.
2. **Detect** overlaps from the per-cell `M_COVR CATCOV=1` polygons: a
   bounding-box pre-filter in Python, then one GDAL SQLite-dialect
   self-join (`ST_Intersects`, `SUM(ST_Area(ST_Intersection))`) for the
   exact overlap area. Pairs under `SAME_BAND_OVERLAP_MIN_AREA` (1e-4 deg²,
   about 1 km²) are the edge slivers adjacent legacy cells legitimately
   share (measured 2.3e-6 deg² for US2EC03M/US2EC04M) and are recorded but
   ignored. Detection is cached in the record file and reruns only when a
   cell's M_COVR export changes.
3. **Resolve** a pair only when one side is legacy and the other reschemed.
   Every layer of the legacy cell, including its own `M_COVR`, is clipped
   by the complement of the union of the overlapping reschemed cells'
   CATCOV=1 footprints (`build_clip_complement` + `erase_layer`, the same
   machinery as the band-level erase). The clipped copies live in
   `data/geojson/bandN.resolved/` and replace the originals at
   consolidation; unaffected cells are untouched and bands without a
   resolvable pair pay nothing. A legacy cell fully inside reschemed
   coverage contributes nothing.
4. **Report.** Console lines per resolved pair with features removed, a
   `WARNING` for every unresolved pair (both legacy, both reschemed, or
   ambiguous bands 3-6), and `data/merged/bandN/.same-band-overlaps.json`
   with rule, inputs, pairs, resolved, unresolved and slivers, for
   reporting to NOAA.

Verify with `check-tile-duplicates.py` (0 duplicates, 1 cell per tile at
z9-12 at Chicago and Block Island) and `verify-r2-charts.py` on CI output.

## How other software copes

Checked 2026-09-04. Nobody publishes a rule for this case, because the
standard says it must not occur; what exists is generic overlap handling.

**OpenCPN** (`gui/src/quilt.cpp`, master). The quilt's chart stack is sorted
by compilation scale, larger scale on top (`CompareScales`, "Primary: scale
(smaller scale value means larger scale chart)"), and the comment on that
function states the tie rule plainly: "Equal scale charts will be stacked
indiscriminately." Each chart's coverage region is its M_COVR CATCOV=1 area
minus its CATCOV=2 areas. When rendering with OpenGL and no overlay cells,
every larger-scale chart's region is subtracted from the smaller-scale
charts below it ("fetch and subtract regions for all larger scale charts"),
so each screen area is drawn from exactly one chart and no duplicate objects
are rendered. For two overlapping cells of the *same* scale the winner is
whichever sorts first: deterministic for a given chart database, but not
chosen on any chart property. This is a render-time quilt, so OpenCPN never
has to merge features; the equivalent for a tile pipeline is to do the same
subtraction before tiling.

**NOAA ENC Direct to GIS.** NOAA's own GIS export merges "S-57 object classes
from all NOAA ENCs into seamless layers" per scale band ("a composite of the
S-57 object class COALNE from all current Harbour ENCs"). The help page says
nothing about overlap within a band; on the published data a straight merge
of all current cells reproduces the duplicates.

**s57-tiler** (wdantuma, the S-57 to vector tiles converter for freeboard-sk).
Reads each cell's usage band (`DSID_INTU`) and compilation scale
(`DSPM_CSCL`) to set zoom ranges, gates features by SCAMIN/SCAMAX, and uses
M_COVR only for the cell's bounding box. Every cell whose bounds intersect a
tile contributes its features. No overlap handling, so it has the same
duplication.

**ECDIS.** Per IC-ENC, behaviour varies by manufacturer: some pick the larger
compilation scale automatically, others draw both. Type-approval (IEC 61174)
does not test this case because S-57 forbids it.

Net: the only working precedent is OpenCPN's, "largest compilation scale wins,
ties arbitrary", applied by subtracting the winner's coverage from the loser.
On this data that rule picks the reschemed cell at Block Island and the
legacy cell at Boston (US2EC04M 1:675,000 versus US2ATLPF 1:700,000).

## Reporting upstream

Because this is a producer-side error under S-57 2.2, it is worth reporting to
NOAA through their nautical inquiry channel with the cell pairs listed above.
Until NOAA cuts back the legacy cells, every downstream product built from the
district zips inherits the overlap.

## Sources

- IC-ENC Policy D10, "Overlapping Data / Areas of Responsibility", ICE-P1 v3,
  2 Nov 2017: <https://iho.int/uploads/user/Inter-Regional%20Coordination/WEND-WG/WENDWG%20Repository/ICE-P1-D10%20-%20Overlapping%20Data%20%20Areas%20of%20Responsibility_FINAL_v3_20171102.pdf>
  (quotes S-57 Appendix B.1 clause 2.2 and IHB CL 47/2004)
- IHO S-57 Appendix B.1, ENC Product Specification:
  <https://iho.int/uploads/user/pubs/standards/s-57/20ApB1.pdf>
- Nyberg, Harmon, Pe'eri, Catoire, "An Overview of the NOAA ENC Re-scheming
  Plan", IHR, 30 Nov 2020:
  <https://ihr.iho.int/articles/semi-automated-generation-of-depth-contours-for-encs-2/>
- NOAA ENC Design Handbook, 1 June 2024 (naming convention, standard scales):
  <https://www.nauticalcharts.noaa.gov/publications/docs/enc-design-handbook.pdf>
- NOAA, Rescheming and Improving Electronic Navigational Charts:
  <https://nauticalcharts.noaa.gov/charts/rescheming-and-improving-electronic-navigational-charts.html>
- NOAA ENC product catalog (cell editions, scales, dates, footprints):
  <https://charts.noaa.gov/ENCs/ENCProdCat_19115.xml>
- IHO S-65, ENC Production, Maintenance and Distribution Guidance, ed 2.1.0,
  May 2017 (edge matching, re-issues; no overlap selection rule):
  <https://iho.int/iho_pubs/standard/S-65/S-65_ed2%201%200_June17.pdf>
- OpenCPN quilting source, `gui/src/quilt.cpp` (master):
  <https://github.com/OpenCPN/OpenCPN/blob/master/gui/src/quilt.cpp>
- NOAA ENC Direct to GIS help: <https://nauticalcharts.noaa.gov/learn/encdirect/>
- s57-tiler (S-57 to vector tiles for freeboard-sk):
  <https://github.com/wdantuma/s57-tiler>
- IHO S-57 Appendix B.1 §5.7 (data set cancellation: update with DSID only, EDTN = 0):
  <https://iho.int/uploads/user/pubs/standards/s-57/20ApB1.pdf>
- GDAL S-57 driver (updates applied on the fly, `UPDATES=APPLY`):
  <https://gdal.org/en/stable/drivers/vector/s57.html>
- Measurements: `verify-r2-charts.py` report of 2026-09-04 and
  `check-tile-duplicates.py` on the published district files.

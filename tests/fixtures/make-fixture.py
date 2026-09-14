#!/usr/bin/env python3
"""
make-fixture.py — build tests/fixtures/mini-district.zip from real NOAA cells

The end-to-end test needs a tiny "district" that still exercises the
by-band pipeline: a band 4 cell, band 5 cells nested inside it (so the
finer-wins erase and the band 4 extension both have work to do) and one
cancelled cell (DSID EDTN=0 after its update) that must be dropped.

Usage:
  make-fixture.py <ENC_ROOT dir or NOAA district zip> [-o OUT.zip] [CELL ...]

Default cells: US4NY1BY US5RI1AC US5RI1AD US5RI1AE US5NJ30M (from
01CGD_ENCs.zip). A custom cell list must name its own output with -o;
the pipeline test pins those five cells, so mini-district.zip is only
ever written from the default list. Only the .000 base file and .NNN
updates are copied;
the .TXT notices are not needed. The zip mirrors NOAA's layout,
ENC_ROOT/<CELL>/<CELL>.NNN, so stage_input treats it like a district.
"""
import sys
import zipfile
from pathlib import Path

DEFAULT_CELLS = ["US4NY1BY", "US5RI1AC", "US5RI1AD", "US5RI1AE", "US5NJ30M"]
OUT = Path(__file__).resolve().parent / "mini-district.zip"


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    src = Path(sys.argv[1])
    rest = sys.argv[2:]
    out = OUT
    if "-o" in rest:
        i = rest.index("-o")
        if i + 1 >= len(rest):
            sys.exit("-o needs a path")
        out = Path(rest[i + 1])
        del rest[i:i + 2]
    cells = [c.upper() for c in rest] or DEFAULT_CELLS
    if cells != DEFAULT_CELLS and out.resolve() == OUT:
        sys.exit("custom cells would overwrite the canonical fixture "
                 f"{OUT.name}; pass -o <other.zip>")
    members = {}
    if src.is_file():
        with zipfile.ZipFile(src) as zf:
            for info in zf.infolist():
                p = Path(info.filename)
                cell = p.stem.upper()
                if cell in cells and p.suffix[1:].isdigit():
                    members[f"ENC_ROOT/{cell}/{p.name}"] = zf.read(info)
    else:
        for cell in cells:
            d = src / cell
            for f in sorted(d.glob(f"{cell}.[0-9][0-9][0-9]")):
                members[f"ENC_ROOT/{cell}/{f.name}"] = f.read_bytes()
    missing = [c for c in cells if not any(f"/{c}/" in k for k in members)]
    if missing:
        sys.exit(f"cells not found in {src}: {' '.join(missing)}")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(members):
            zf.writestr(name, members[name])
    print(f"wrote {out} ({out.stat().st_size // 1024} KB, "
          f"{len(members)} files, {len(cells)} cells)")


if __name__ == "__main__":
    main()

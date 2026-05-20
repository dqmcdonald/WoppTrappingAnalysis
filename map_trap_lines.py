#!/usr/bin/env python
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pandas",
#   "pyproj",
#   "shapely",
# ]
# ///
"""Map each trap in a Trap.NZ visit-log CSV to its nearest named line.

Some traps aren't assigned to a line in Trap.NZ, leaving the `line` column blank,
so line membership has to be inferred spatially. This assigns each unique trap to
the closest line defined in a Trap.NZ "lines" export (WKT geometries) and writes
a lookup CSV with a per-trap distance for QC.

Coordinates are transformed from WGS84 (EPSG:4326) to NZTM2000 (EPSG:2193) so
distances come out in metres.

Results are merged into a cumulative TrapLineAssignments.csv keyed on `trap nid`:
traps in the current run are added or refreshed, while assignments from previous
runs (e.g. other trap networks) are preserved. Re-running the same trap file just
updates its own rows.

Usage:
    map_trap_lines.py <traps.csv> <lines.csv>                 # merge into TrapLineAssignments.csv
    map_trap_lines.py <traps.csv> <lines.csv> --max-dist 50   # also flag traps >50 m from any line
"""

import argparse
from pathlib import Path

import pandas as pd
from pyproj import Transformer
from shapely import wkt
from shapely.geometry import Point
from shapely.ops import transform as shp_transform

WGS84 = "EPSG:4326"
NZTM = "EPSG:2193"
OUTPUT_CSV = "TrapLineAssignments.csv"


def load_lines(path: Path, to_nztm):
    """Return list of (name, colour, nztm_geometry) from a Trap.NZ lines export."""
    df = pd.read_csv(path)
    lines = []
    for _, row in df.iterrows():
        geom = wkt.loads(row["wkt"])
        lines.append((row["name"], row.get("colour", ""), shp_transform(to_nztm, geom)))
    return lines


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("traps", help="Trap.NZ visit-log CSV")
    ap.add_argument("lines", help="Trap.NZ lines export with WKT geometries")
    ap.add_argument("--max-dist", type=float, default=None,
                    help="Warn about traps farther than this many metres from any line")
    args = ap.parse_args()

    traps_path = Path(args.traps)
    out_path = Path(OUTPUT_CSV)

    # always_xy -> (lon, lat) in, (easting, northing) out
    to_nztm = Transformer.from_crs(WGS84, NZTM, always_xy=True).transform

    lines = load_lines(Path(args.lines), to_nztm)

    traps = (pd.read_csv(traps_path)
             .drop_duplicates("trap nid")
             .reset_index(drop=True))

    records = []
    for _, t in traps.iterrows():
        e, n = to_nztm(t["longitude"], t["latitude"])
        pt = Point(e, n)
        name, colour, dist = min(
            ((nm, col, pt.distance(geom)) for nm, col, geom in lines),
            key=lambda x: x[2],
        )
        records.append({
            "trap nid": t["trap nid"],
            "code": t["code"],
            "trap type": t["trap type"],
            "latitude": t["latitude"],
            "longitude": t["longitude"],
            "easting": round(e, 3),
            "northing": round(n, 3),
            "line": name,
            "line_colour": colour,
            "distance_m": round(dist, 2),
        })

    out = pd.DataFrame(records)

    # Merge into the cumulative directory: drop prior rows for these traps,
    # keep everything else, then append this run.
    n_replaced = 0
    combined = out
    if out_path.exists():
        prev = pd.read_csv(out_path)
        kept = prev[~prev["trap nid"].isin(out["trap nid"])]
        n_replaced = len(prev) - len(kept)
        combined = pd.concat([kept, out], ignore_index=True)

    combined = combined.sort_values(["line", "code"]).reset_index(drop=True)
    combined.to_csv(out_path, index=False)

    updated = "updated" if n_replaced else "added"
    print(f"Mapped {len(out)} traps from {traps_path.name} "
          f"({n_replaced} {updated}) -> {out_path} now holds {len(combined)} traps")
    print("\nTraps per line (whole directory):")
    print(combined["line"].value_counts().to_string())
    print(f"\nThis run's distance to assigned line (m): "
          f"min {out['distance_m'].min():.2f}, "
          f"median {out['distance_m'].median():.2f}, "
          f"max {out['distance_m'].max():.2f}")

    if args.max_dist is not None:
        far = out[out["distance_m"] > args.max_dist]
        if len(far):
            print(f"\n{len(far)} trap(s) farther than {args.max_dist} m from any line:")
            print(far[["code", "trap type", "line", "distance_m"]].to_string(index=False))
        else:
            print(f"\nAll traps within {args.max_dist} m of their assigned line.")


if __name__ == "__main__":
    main()

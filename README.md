# Wopp Trapping Analysis

Analysis of pest-trapping data from trap networks around Whakaraupō/Lyttelton Harbour, New Zealand.

Data is exported from [Trap.NZ](https://trap.nz) in their standard CSV format.

## Requirements

Python 3.12, plus the packages listed in `requirements.txt`.

### Option A — uv (recommended, no setup needed)

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) once:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # macOS / Linux
# or: winget install astral-sh.uv                  # Windows
```

Then just run the script — `uv` reads the inline dependency block and handles everything automatically:

```bash
uv run report_traps.py traps.csv
```

### Option B — pip + venv (traditional)

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python report_traps.py traps.csv
```

## Assigning traps to lines

Some traps aren't assigned to a line in Trap.NZ, so the `line` column in their
visit logs is left blank. `map_trap_lines.py` infers each trap's line spatially,
assigning it to the **nearest** named line in a Trap.NZ *lines* export (WKT
geometries). Coordinates are projected from WGS84 to NZTM2000 so distances come
out in metres.

```bash
# Map traps to their nearest line -> TrapLineAssignments.csv
python map_trap_lines.py DHCoastal.csv DHCoastalLines.csv

# Also flag any trap further than 50 m from its assigned line (a QC check)
python map_trap_lines.py DHCoastal.csv DHCoastalLines.csv --max-dist 50
```

Results are merged into a cumulative `TrapLineAssignments.csv` keyed on `trap nid`:
re-running a trap file refreshes only its own rows, while assignments from other
networks are preserved. Each row records `trap nid, code, trap type, latitude,
longitude, easting, northing, line, line_colour, distance_m` (the last for QC).
`report_traps.py` reads this file to resolve trap lines (see below).

This step needs `pyproj` and `shapely` (both in `requirements.txt`; `uv run`
installs them automatically).

## Generating reports

```bash
# Report for a single CSV (all analyses)
python report_traps.py traps.csv

# Report for every *.csv in the current directory
python report_traps.py -a

# Exclude specific analyses (all are included by default)
python report_traps.py traps.csv --no-species --no-status

# Control the number of traps shown in ranked analyses
python report_traps.py traps.csv --top-n 10

# Limit the report to a single line -> traps_<line>_report.pdf
python report_traps.py traps.csv --line Green

# Available --no-X flags to suppress individual sections:
#   --no-species              Catches by species
#   --no-bait                 Catch rate by bait type (min. 5 visits)
#   --no-bait-traptype        Catches per bait, stacked by trap type
#   --no-species-bait         Bait-by-species catch-count heatmap
#   --no-over-time            Catches per week over time (with linear trend line)
#   --no-rate-over-time       Weekly catch rate (% of visits) over time
#   --no-species-over-time    Catches per week broken down by species
#   --no-cumulative           Cumulative catches over time by species
#   --no-catch-rates          Top-N traps by catch rate (min. 3 visits)
#   --no-catch-concentration  Pareto curves by species: % of traps vs cumulative % of catches
#   --no-inter-catch          Inter-catch interval box plot for top-N traps
#   --no-interval             Catch rate / per-day yield vs checking interval
#   --no-spatial              Spatial clustering (Moran's I) of trap catch rates
#   --no-sprung               Top-N traps most often found sprung with no catch
#   --no-bait-missing         Top-N traps most often found with bait missing
#   --no-status               Trap status distribution

# --top-n controls the N in catch-rates, inter-catch, sprung, and bait-missing (default: 20)
# --line LINE restricts the report to a single line by name
```

Each trap's line is taken from `TrapLineAssignments.csv` (matched on `trap nid`),
falling back to the `line` value in the visit record when a trap isn't listed there.
The lines present are shown in the report summary.

Output is written as `<csvname>_report.pdf` alongside each input file
(`<csvname>_<line>_report.pdf` when `--line` is used).

## Report contents

Each PDF includes (subject to the analysis flags chosen):

- Summary table (visits, traps, catches, catch rate, date range, lines present)
- Catches by species
- Catches by bait: catch rate (% of baited visits resulting in a catch) per bait type, with summary table
- Catches by bait and trap type: total catches per bait, stacked by the trap type that made each catch
- Catches by bait and species: heatmap of catch counts for each bait/species pairing
- Catches over time (weekly, total) with a linear trend line
- Catch rate over time (weekly catches as % of visits) with a linear trend line
- Catches over time (weekly, broken down by species)
- Cumulative catches over time by species
- Top-N traps by catch rate (min. 3 visits), with summary table
- Catch concentration: Pareto curves per species (% of traps vs cumulative % of catches, 80% reference line)
- Spatial clustering: Moran scatterplot of each trap's catch rate vs its nearest neighbours', with Moran's I and a permutation p-value
- Inter-catch interval box plot for top-N traps by catch rate
- Catch rate vs checking interval: per-visit catch rate and catches-per-trap-day against the gap since the last check
- Top-N traps most often found sprung with no catch (as % of visits)
- Top-N traps most often found with bait missing (as % of visits)
- Trap status breakdown (bait OK / bad / missing / sprung)

All dates are shown in dd/mm/yyyy format. Page numbers appear on every page.

## Data normalisation

- **Rat species**: "Rat - Ship" and "Rat - Norway" are merged into "Rat".
- **Retired traps**: visits recorded under a `(retired)` code (e.g. `DHM004 (retired)`)
  are merged into the corresponding active trap record rather than excluded.
- **Bait**: the `bait type` field may list several comma-separated baits for a
  single visit (e.g. `"Peanut butter, Dehydrated Rabbit"`), in inconsistent
  order. For the catches-by-bait analysis these are split into individual
  ingredients, each counted separately, so a multi-bait visit (and any catch on
  it) contributes to every bait listed. Visits with no recorded bait are
  excluded from that analysis, and baits seen on fewer than 5 visits are dropped
  as too sparse to rank.

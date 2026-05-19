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

## Usage

```bash
# Report for a single CSV (all analyses)
python report_traps.py traps.csv

# Report for every *.csv in the current directory
python report_traps.py -a

# Only include specific analyses
python report_traps.py traps.csv --species --over-time

# Control the number of traps shown in ranked analyses
python report_traps.py traps.csv --top-n 10

# Available analysis flags (combine freely; omit all to include everything):
#   --species              Catches by species
#   --over-time            Catches per week over time (with linear trend line)
#   --species-over-time    Catches per week broken down by species
#   --cumulative           Cumulative catches over time by species
#   --catch-rates          Top-N traps by catch rate (min. 3 visits)
#   --catch-concentration  Pareto curves by species: % of traps vs cumulative % of catches
#   --inter-catch          Inter-catch interval box plot for top-N traps
#   --sprung               Top-N traps most often found sprung with no catch
#   --status               Trap status distribution

# --top-n controls the N in catch-rates, inter-catch, and sprung (default: 20)
```

Output is written as `<csvname>_report.pdf` alongside each input file.

## Report contents

Each PDF includes (subject to the analysis flags chosen):

- Summary table (visits, traps, catches, catch rate, date range)
- Catches by species
- Catches over time (weekly, total) with a linear trend line
- Catches over time (weekly, broken down by species)
- Cumulative catches over time by species
- Top-N traps by catch rate (min. 3 visits), with summary table
- Catch concentration: Pareto curves per species (% of traps vs cumulative % of catches, 80% reference line)
- Inter-catch interval box plot for top-N traps by catch rate
- Top-N traps most often found sprung with no catch (as % of visits)
- Trap status breakdown (bait OK / bad / missing / sprung)

All dates are shown in dd/mm/yyyy format. Page numbers appear on every page.

## Data normalisation

- **Rat species**: "Rat - Ship" and "Rat - Norway" are merged into "Rat".
- **Retired traps**: visits recorded under a `(retired)` code (e.g. `DHM004 (retired)`)
  are merged into the corresponding active trap record rather than excluded.

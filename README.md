# Wopp Trapping Analysis

Analysis of pest-trapping data from trap networks around Whakaraupō/Lyttelton Harbour, New Zealand.

Data is exported from [Trap.NZ](https://trap.nz) in their standard CSV format.

## Requirements

Python 3.12 with the following packages:

- pandas
- matplotlib
- numpy
- reportlab

Install dependencies:

```
pip install reportlab
```

## Usage

```bash
# Report for a single CSV (all analyses)
python report_traps.py traps.csv

# Report for every *.csv in the current directory
python report_traps.py -a

# Only include specific analyses
python report_traps.py traps.csv --species --over-time

# Available analysis flags (combine freely; omit all to include everything):
#   --species     Catches by species
#   --over-time   Catches per week over time
#   --top-traps   Top traps by total catches
#   --status      Trap status distribution
```

Output is written as `<csvname>_report.pdf` alongside each input file.

## Report contents

Each PDF includes (subject to the analysis flags chosen):

- Summary table (visits, traps, catches, catch rate, date range)
- Catches by species
- Catches over time (weekly)
- Top traps by catches
- Trap status breakdown (bait OK / bad / missing / sprung)

Dates throughout the report and graphs are shown in dd/mm/yyyy format.

## Species normalisation

"Rats-Ship" and "Rats-Norway" are both counted as "Rats" in the species breakdown.

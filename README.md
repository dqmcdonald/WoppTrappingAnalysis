# Wopp Trapping Analysis

Analysis of pest-trapping data from trap networks around Whakaraupoū/Lyttleton Harbour, New Zealand.

Data is exported from [Trap.NZ](https://trap.nz) in their standard CSV format.

## Requirements

Uses the `tf` virtualenv (`/Users/que/venvs/tf`) with Python 3.12 and the following packages:

- pandas
- matplotlib
- numpy
- reportlab

Install reportlab if not already present:

```
/Users/que/venvs/tf/bin/pip install reportlab
```

## Usage

```bash
# Report for a single CSV
/Users/que/venvs/tf/bin/python report_traps.py traps.csv

# Report for every *.csv in the current directory
/Users/que/venvs/tf/bin/python report_traps.py -a
```

Output is written as `<csvname>_report.pdf` alongside each input file.

## Report contents

Each PDF includes:

- Summary table (visits, traps, catches, catch rate, date range)
- Catches by species
- Catches and catch-rate by trap type
- Catches over time (weekly)
- Top traps by catches
- Trap status breakdown (bait OK / bad / missing / sprung)
- Per trap-line breakdown *(only rendered when trap lines are present in the data)*

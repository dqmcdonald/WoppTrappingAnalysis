# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this directory is

Analysis workspace for pest-trapping data from two trap networks in Banks Peninsula, NZ:
- `DHCoastal.csv` — Diamond Harbour Coastal traps (codes `DH*`).
- `DHStoddartPoint.csv` — Stoddart Point traps (codes `SP*`, grouped by trap line `TL1`, `TL2`, ...).

Both files are Trap.NZ-style exports.

## Tooling

Analysis is done in **Python**, using the user's `tf` virtualenv at `/Users/que/venvs/tf` (Python 3.12, pandas / numpy / matplotlib already installed). Invoke it directly via its interpreter rather than activating it in a subshell:

```
/Users/que/venvs/tf/bin/python script.py
```

Don't create per-project venvs or pin new dependency files unless asked — reuse `tf`.

## Data shape

25 columns, identical schema in both files:
`line, trap nid, code, trap type, Trap sub type description, tags, latitude, longitude, easting, northing, nid, date, status, initial bait, rebaited, bait type, Bait details, recorded by, strikes, species caught, sex, maturity, trap condition, notes, Images`

Key things that aren't obvious from a glance at the header:
- One row = one **visit** to a trap, not one trap. The same physical trap reappears across dates.
- `trap nid` is the stable trap identifier; `nid` is the per-visit record id.
- `code` is the human-readable trap label (e.g. `DHR006`, `SPR-001`). Suffix letter encodes trap type: `R` = D-Rat, `D` = DOC 200, `T` = Trapinator, `M` = Mouse trap.
- `strikes` is the catch count for that visit (0 or 1 in the current data).
- Several text fields contain commas inside quotes (e.g. `"Peanut butter, Dehydrated Rabbit"`). Use a real CSV parser; `awk -F,` will mis-split.
- Coordinates are given as both WGS84 lat/lon and NZTM (`easting`, `northing`).
- `line` is populated only for Stoddart Point; Diamond Harbour Coastal leaves it blank.

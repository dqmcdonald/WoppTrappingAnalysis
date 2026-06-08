#!/usr/bin/env python
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "matplotlib",
#   "numpy",
#   "pandas",
#   "reportlab",
# ]
# ///
"""Generate a styled PDF report from a Trap.NZ visit-log CSV.

Each trap's line is resolved from TrapLineAssignments.csv (matched on `trap nid`),
falling back to the `line` value in the visit record when a trap is absent there.
The lines present are listed in the report summary.

Usage:
    report_traps.py <csv>                          # all analyses -> <stem>_report.pdf
    report_traps.py -a                             # every *.csv in cwd
    report_traps.py <csv> --no-species             # all except species
    report_traps.py <csv> --line Green             # limit to the "Green" line -> <stem>_Green_report.pdf
    report_traps.py <csv> --no-inter-catch --top-n 10  # top 10 by catch rate, skip inter-catch

Analysis flags (all included by default; use --no-X to exclude):
    --no-species              Exclude catches by species
    --no-bait                 Exclude catch rate by bait type
    --no-bait-traptype        Exclude catches per bait stacked by trap type
    --no-species-bait         Exclude bait-by-species catch-count heatmap
    --no-over-time            Exclude catches per week over time (with linear trend)
    --no-rate-over-time       Exclude weekly catch rate (% of visits) over time
    --no-species-over-time    Exclude catches per week broken down by species
    --no-cumulative           Exclude cumulative catches over time by species
    --no-catch-rates          Exclude top-N traps by catch rate (min. 3 visits)
    --no-catch-concentration  Exclude Pareto curves by species: % of traps vs cumulative % of catches
    --no-inter-catch          Exclude inter-catch interval box plot for top-N traps
    --no-interval             Exclude catch rate / per-day yield vs checking interval
    --no-spatial              Exclude spatial clustering (Moran's I) of catch rates
    --no-underperformers      Exclude watchlist of traps lagging nearby same-type traps
    --no-sprung               Exclude top-N traps most often found sprung with no catch
    --no-bait-missing         Exclude top-N traps most often found with bait missing
    --no-status               Exclude trap status distribution

Other options:
    --line LINE            Limit the analysis to a single line (by name)
    --top-n N              Number of traps shown in catch-rates, inter-catch,
                           and sprung analyses (default: 20)
    -a, --all              Process every *.csv in the current directory
"""

import argparse
import glob
import io
import re
import sys
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

PLOT_DPI = 150
PLOT_W_IN, PLOT_H_IN = 6.5, 4.0
CATCH_RATE_MIN_VISITS = 3
# A bait must appear on at least this many visits before its catch rate is
# reported. Without a floor, a bait used once or twice produces a meaningless
# 0%/100% rate that swamps the chart; 5 keeps every routinely-used bait while
# dropping the long tail of one-off experiments.
BAIT_MIN_VISITS = 5
DEFAULT_TOP_N = 20

# Checking-interval analysis: how many days passed since a trap was last
# checked, grouped into bins. Same-day re-checks (interval 0) and each trap's
# very first visit (no prior check) are excluded.
INTERVAL_BINS = [0, 3, 7, 14, 21, 28, float("inf")]
INTERVAL_LABELS = ["1–3", "4–7", "8–14", "15–21", "22–28", "29+"]

# Spatial autocorrelation: each trap's catch rate is compared against the mean
# of its SPATIAL_KNN nearest neighbours (by NZTM easting/northing). Needs at
# least SPATIAL_MIN_TRAPS traps (each with >= CATCH_RATE_MIN_VISITS visits) to
# be meaningful.
SPATIAL_KNN = 5
SPATIAL_MIN_TRAPS = 5
SPATIAL_PERMUTATIONS = 999

# Underperforming-trap watchlist: a trap is flagged when its catch rate is at
# least UNDERPERFORMER_MIN_GAP percentage points below the mean of its nearest
# neighbours *of the same trap type* (so a possum trap is judged against nearby
# possum traps, not against rodent traps). A trap type needs at least
# UNDERPERFORMER_MIN_TYPE_TRAPS traps in the network to be comparable at all.
UNDERPERFORMER_MIN_GAP = 5.0
UNDERPERFORMER_MIN_TYPE_TRAPS = 4

plt.style.use("seaborn-v0_8")
plt.rcParams.update(
    {
        "figure.figsize": (PLOT_W_IN, PLOT_H_IN),
        "figure.dpi": PLOT_DPI,
        "font.size": 10,
        "axes.titlesize": 12,
        "lines.markersize": 3,
        "lines.linewidth": 0.75,
    }
)


# ---------- data ----------

RAT_ALIASES = {"Rat - Ship", "Rat - Norway", "Rat - Kiore"}
# Bait names that mean the same thing for our purposes are folded together so
# they share a single row in the catches-by-bait analysis. Keyed alias -> canonical.
BAIT_ALIASES = {
    "Fish": "Fish Pellets",
    "Salted Rabbit": "Dehydrated Rabbit",
    "Goodnature Nut Butter": "Peanut butter",
}
LINE_ASSIGNMENTS_CSV = "TrapLineAssignments.csv"


class LineNotFound(Exception):
    """Requested --line is not present in the (resolved) data."""


def resolve_lines(df: pd.DataFrame, assignments_path: Path) -> pd.DataFrame:
    """Set each visit's line, preferring TrapLineAssignments.csv (matched on
    `trap nid`) and falling back to the record's own `line` value."""
    if "line" not in df.columns:
        df["line"] = pd.Series(pd.NA, index=df.index, dtype="string")
    if assignments_path.is_file():
        a = pd.read_csv(assignments_path, usecols=["trap nid", "line"])
        amap = dict(zip(
            a["trap nid"].astype("string"),
            a["line"].astype("string").str.strip(),
        ))
        assigned = df["trap nid"].astype("string").map(amap)
        df["line"] = assigned.fillna(df["line"])
    return df


def load_visits(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["date"])
    df["strikes"] = pd.to_numeric(df["strikes"], errors="coerce").fillna(0).astype(int)
    for col in ["code", "trap type", "status", "species caught", "line"]:
        if col in df.columns:
            df[col] = df[col].astype("string").str.strip()
    df["species caught"] = df["species caught"].replace(
        {alias: "Rat" for alias in RAT_ALIASES}
    )
    df["code"] = df["code"].str.replace(r"\s*\(retired\)", "", regex=True, case=False)
    return df


def compute_stats(df: pd.DataFrame) -> dict:
    total_visits = len(df)
    total_catches = int(df["strikes"].sum())
    rate = (total_catches / total_visits * 100) if total_visits else 0.0
    species = (
        df.loc[df["strikes"] > 0, "species caught"]
        .replace({"None": pd.NA, "": pd.NA})
        .dropna()
        .value_counts()
    )
    status_counts = df["status"].value_counts()

    by_trap = df.groupby("code").agg(
        visits=("strikes", "size"),
        catches=("strikes", "sum"),
    )
    by_trap = by_trap[by_trap["visits"] >= CATCH_RATE_MIN_VISITS].copy()
    by_trap["rate"] = by_trap["catches"] / by_trap["visits"] * 100

    lines_present = sorted(df["line"].dropna().unique().tolist())
    unassigned_traps = int(df.loc[df["line"].isna(), "trap nid"].nunique())

    return {
        "total_visits": total_visits,
        "unique_traps": int(df["trap nid"].nunique()),
        "total_catches": total_catches,
        "rate_pct": rate,
        "date_min": df["date"].min(),
        "date_max": df["date"].max(),
        "species": species,
        "status": status_counts,
        "by_trap_rate": by_trap,
        "by_bait": compute_bait_stats(df),
        "underperformers": compute_underperformers(df, by_trap),
        "lines": lines_present,
        "unassigned_traps": unassigned_traps,
    }


def explode_bait(df: pd.DataFrame) -> pd.DataFrame:
    """Return one row per (visit, individual bait ingredient).

    The `bait type` field records what was on the trap at a visit. It is often a
    single bait ("Peanut butter") but may list several comma-separated
    ingredients ("Peanut butter, Dehydrated Rabbit"), and the order of those
    ingredients is not consistent across records. To compare baits fairly we
    split on commas and strip surrounding whitespace, so each ingredient is
    counted on its own. A visit baited with two ingredients therefore
    contributes a row — and its catch — to *each* ingredient: we can't tell
    which of the two actually did the catching, so both get the credit. Visits
    with no recorded bait (blank / NaN) drop out entirely. Equivalent bait
    names are folded together via BAIT_ALIASES (e.g. "Fish" -> "Fish Pellets").

    Only the columns needed downstream are retained: `bait`, `strikes`, and the
    `trap type` / `species caught` dimensions callers break the catches down by.
    """
    exploded = df.assign(bait=df["bait type"].str.split(",")).explode("bait")
    exploded["bait"] = exploded["bait"].str.strip().replace(BAIT_ALIASES)
    exploded = exploded[exploded["bait"].notna() & (exploded["bait"] != "")]
    return exploded[["bait", "trap type", "species caught", "strikes"]]


def compute_bait_stats(df: pd.DataFrame, min_visits: int = BAIT_MIN_VISITS) -> pd.DataFrame:
    """Per-bait visit count, catch count, and catch rate (catches / visits).

    Catch *rate* — not raw catch count — is the fair comparison between baits:
    a bait used on hundreds of visits will rack up more total catches than a
    rarely-used one simply through exposure, regardless of how effective it is.
    Baits seen on fewer than `min_visits` visits are dropped as too noisy to
    rank (see BAIT_MIN_VISITS). The result is indexed by bait name.
    """
    exploded = explode_bait(df)
    by_bait = exploded.groupby("bait").agg(
        visits=("strikes", "size"),
        catches=("strikes", "sum"),
    )
    by_bait = by_bait[by_bait["visits"] >= min_visits].copy()
    by_bait["rate"] = by_bait["catches"] / by_bait["visits"] * 100
    return by_bait


def compute_underperformers(
    df: pd.DataFrame,
    by_trap: pd.DataFrame,
    k: int = SPATIAL_KNN,
    min_gap: float = UNDERPERFORMER_MIN_GAP,
) -> pd.DataFrame:
    """Traps catching far less than nearby traps of the same type.

    For each trap (already filtered to those with enough visits in `by_trap`) we
    find its `k` nearest neighbours *of the same trap type* by NZTM coordinates,
    and compare its catch rate to their mean. Restricting to the same type is
    what makes the comparison fair: trap types have structurally different catch
    rates (a possum trap will never match a rat trap's hit rate), so judging a
    trap only against comparable nearby traps isolates a likely siting or
    maintenance problem from the trap type itself.

    Returns the flagged traps — those at least `min_gap` percentage points below
    their same-type neighbours — sorted worst-first, with `neighbour_mean` and
    `gap` (neighbour_mean - rate) columns added. Trap types with fewer than
    UNDERPERFORMER_MIN_TYPE_TRAPS traps are skipped (too few to compare), as are
    traps without coordinates.
    """
    out_cols = list(by_trap.columns) + ["trap type", "neighbour_mean", "gap"]
    if by_trap.empty:
        return pd.DataFrame(columns=out_cols)

    coords = df.groupby("code")[["easting", "northing"]].first().apply(
        pd.to_numeric, errors="coerce"
    )
    trap_type = df.groupby("code")["trap type"].first()
    sub = by_trap.join(coords).join(trap_type).dropna(
        subset=["easting", "northing", "trap type"]
    )

    frames = []
    for _, grp in sub.groupby("trap type", observed=True):
        if len(grp) < UNDERPERFORMER_MIN_TYPE_TRAPS:
            continue
        xy = grp[["easting", "northing"]].to_numpy(float)
        rate = grp["rate"].to_numpy(float)
        kk = min(k, len(grp) - 1)
        dist2 = ((xy[:, None, :] - xy[None, :, :]) ** 2).sum(-1)
        np.fill_diagonal(dist2, np.inf)
        nn = np.argsort(dist2, axis=1)[:, :kk]
        g = grp.copy()
        g["neighbour_mean"] = rate[nn].mean(axis=1)
        g["gap"] = g["neighbour_mean"] - g["rate"]
        frames.append(g)

    if not frames:
        return pd.DataFrame(columns=out_cols)
    flagged = pd.concat(frames)
    flagged = flagged[flagged["gap"] >= min_gap]
    return flagged.sort_values("gap", ascending=False)


# ---------- plots ----------


def _fig_to_buf(fig, tight: bool = True) -> io.BytesIO:
    buf = io.BytesIO()
    kwargs = {} if tight else {"bbox_inches": "tight"}
    if tight:
        fig.tight_layout()
    fig.savefig(buf, format="png", dpi=PLOT_DPI, **kwargs)
    plt.close(fig)
    buf.seek(0)
    return buf


def plot_species(species: pd.Series) -> io.BytesIO:
    fig, ax = plt.subplots()
    if species.empty:
        ax.text(0.5, 0.5, "No catches recorded", ha="center", va="center")
        ax.set_axis_off()
    else:
        s = species.sort_values()
        ax.barh(s.index, s.values, color="#a23b3b")
        for i, v in enumerate(s.values):
            ax.text(v, i, f" {v}", va="center")
        ax.set_xlabel("Catches")
        ax.set_title("Catches by species")
    return _fig_to_buf(fig)


def plot_catch_by_bait(by_bait: pd.DataFrame) -> io.BytesIO:
    """Horizontal bar chart of catch rate by bait type.

    Bars are sorted so the most effective bait sits at the top, and each bar is
    annotated with the rate and the underlying "(catches/visits)" so a high rate
    backed by few visits is obvious at a glance. `by_bait` is the frame from
    compute_bait_stats (already filtered to BAIT_MIN_VISITS); an empty frame
    means no bait cleared that threshold, so we draw a placeholder instead.
    """
    fig, ax = plt.subplots()
    if by_bait.empty:
        ax.text(0.5, 0.5, f"No bait recorded on ≥{BAIT_MIN_VISITS} visits",
                ha="center", va="center")
        ax.set_axis_off()
    else:
        s = by_bait.sort_values("rate")
        ax.barh(s.index, s["rate"], color="#6a4a8a")
        for i, (rate, catches, visits) in enumerate(
            zip(s["rate"], s["catches"], s["visits"])
        ):
            ax.text(rate, i, f" {rate:.1f}% ({int(catches)}/{int(visits)})",
                    va="center", fontsize=8)
        ax.set_xlabel("Catch rate (% of visits)")
        ax.set_title(f"Catch rate by bait type (min. {BAIT_MIN_VISITS} visits)")
    return _fig_to_buf(fig)


def plot_catch_by_bait_traptype(df: pd.DataFrame, by_bait: pd.DataFrame) -> io.BytesIO:
    """Stacked bar chart of catch *counts* per bait, split by trap type.

    The companion catch-rate chart can't show this: rates don't add up, so they
    can't be stacked. Here each bar is a bait's total catches, segmented by the
    trap type that made each catch — revealing, e.g., that peanut butter's
    catches come mostly from rat traps while possum dough's come from
    Trapinators. Only baits that appear in `by_bait` (i.e. cleared the
    BAIT_MIN_VISITS floor) are shown, so this section lines up with the rate
    chart above it. Bars are ordered by total catches, largest at the top.
    """
    exploded = explode_bait(df)
    catches = exploded[(exploded["strikes"] > 0) & exploded["bait"].isin(by_bait.index)]
    # rows = bait, cols = trap type, cells = number of catches
    matrix = catches.groupby(["bait", "trap type"]).size().unstack(fill_value=0)

    fig, ax = plt.subplots()
    if matrix.empty or int(matrix.to_numpy().sum()) == 0:
        ax.text(0.5, 0.5, "No catches recorded for ranked baits",
                ha="center", va="center")
        ax.set_axis_off()
        return _fig_to_buf(fig)

    # ascending total so the biggest bait sits at the top of the horizontal axis
    matrix = matrix.loc[matrix.sum(axis=1).sort_values().index]
    left = np.zeros(len(matrix))
    for trap_type in matrix.columns:
        vals = matrix[trap_type].to_numpy()
        ax.barh(matrix.index, vals, left=left, label=trap_type)
        left += vals

    ax.set_xlabel("Catches")
    ax.set_title("Catches by bait, split by trap type")
    ax.legend(fontsize=8, title="Trap type")
    return _fig_to_buf(fig)


def plot_species_bait_heatmap(df: pd.DataFrame, by_bait: pd.DataFrame) -> io.BytesIO:
    """Heatmap of catch counts for each (bait, species) pairing.

    Where the stacked chart asks "which trap type caught it", this asks "which
    species did each bait actually catch" — the cell at (bait, species) is the
    number of that species caught over visits carrying that bait. Colour and the
    printed number both encode the count, so dominant pairings (peanut butter ->
    mouse, possum dough -> possum, dehydrated rabbit -> rat) stand out at a
    glance. Rows and columns are ordered by total catches so the busiest baits
    and species sit top-left. Restricted to the same baits as the charts above
    (those past the BAIT_MIN_VISITS floor); rows/columns are catches only, so
    visits with no catch or no recorded species don't appear.
    """
    exploded = explode_bait(df)
    catches = exploded[(exploded["strikes"] > 0) & exploded["bait"].isin(by_bait.index)].copy()
    catches["species caught"] = catches["species caught"].replace({"None": pd.NA, "": pd.NA})
    catches = catches.dropna(subset=["species caught"])
    matrix = catches.groupby(["bait", "species caught"]).size().unstack(fill_value=0)

    fig, ax = plt.subplots()
    if matrix.empty or int(matrix.to_numpy().sum()) == 0:
        ax.text(0.5, 0.5, "No identified catches for ranked baits",
                ha="center", va="center")
        ax.set_axis_off()
        return _fig_to_buf(fig)

    # busiest bait (row) and species (column) to the top-left
    matrix = matrix.loc[matrix.sum(axis=1).sort_values(ascending=False).index]
    matrix = matrix[matrix.sum(axis=0).sort_values(ascending=False).index]
    data = matrix.to_numpy()

    ax.grid(False)  # the seaborn style's gridlines would cut across the cells
    im = ax.imshow(data, aspect="auto", cmap="YlOrRd")
    ax.set_xticks(range(len(matrix.columns)), labels=matrix.columns,
                  rotation=45, ha="right")
    ax.set_yticks(range(len(matrix.index)), labels=matrix.index)

    # print the count in each non-zero cell, in whichever colour stays legible
    threshold = data.max() / 2
    for i in range(data.shape[0]):
        for j in range(data.shape[1]):
            count = int(data[i, j])
            if count:
                ax.text(j, i, str(count), ha="center", va="center", fontsize=8,
                        color="white" if data[i, j] > threshold else "black")

    cbar = fig.colorbar(im, ax=ax)
    cbar.set_label("Catches")
    ax.set_xlabel("Species")
    ax.set_ylabel("Bait")
    ax.set_title("Catches by bait and species")
    return _fig_to_buf(fig)


def plot_over_time(df: pd.DataFrame) -> io.BytesIO:
    weekly = (
        df.set_index("date")["strikes"]
        .resample("W-MON", label="left", closed="left")
        .sum()
    )
    fig, ax = plt.subplots()
    ax.plot(weekly.index, weekly.values, marker="o", color="#a23b3b", label="Weekly catches")
    ax.fill_between(weekly.index, weekly.values, alpha=0.15, color="#a23b3b")

    x_num = mdates.date2num(weekly.index.to_pydatetime())
    coeffs = np.polyfit(x_num, weekly.values, 1)
    trend = np.poly1d(coeffs)(x_num)
    ax.plot(weekly.index, trend, linestyle="--", linewidth=1.0, color="#555555",
            alpha=0.8, label="Trend")

    ax.set_ylabel("Catches")
    ax.set_xlabel("Week starting")
    ax.set_title("Catches per week")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m/%Y"))
    ax.legend(fontsize=8)
    fig.autofmt_xdate()
    return _fig_to_buf(fig)


def plot_cumulative_catches(df: pd.DataFrame) -> io.BytesIO:
    catches = df[df["strikes"] > 0].copy()
    catches["species caught"] = catches["species caught"].replace({"None": pd.NA, "": pd.NA})
    catches = catches.dropna(subset=["species caught"])

    fig, ax = plt.subplots()
    if catches.empty:
        ax.text(0.5, 0.5, "No catches recorded", ha="center", va="center")
        ax.set_axis_off()
    else:
        for species in sorted(catches["species caught"].unique()):
            series = (
                catches[catches["species caught"] == species]
                .groupby("date")["strikes"]
                .sum()
                .sort_index()
                .cumsum()
            )
            ax.plot(series.index, series.values, drawstyle="steps-post",
                    label=species)
        ax.set_ylabel("Cumulative catches")
        ax.set_xlabel("Date")
        ax.set_title("Cumulative catches by species")
        ax.legend()
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m/%Y"))
        fig.autofmt_xdate()
    return _fig_to_buf(fig)


def plot_catch_rate_over_time(df: pd.DataFrame) -> io.BytesIO:
    weekly = df.set_index("date").resample("W-MON", label="left", closed="left")
    catches = weekly["strikes"].sum()
    visits = weekly["strikes"].count()
    mask = visits > 0
    rate = catches[mask] / visits[mask] * 100

    fig, ax = plt.subplots()
    ax.plot(rate.index, rate.values, marker="o", color="#a23b3b", label="Weekly catch rate")
    ax.fill_between(rate.index, rate.values, alpha=0.15, color="#a23b3b")

    x_num = mdates.date2num(rate.index.to_pydatetime())
    coeffs = np.polyfit(x_num, rate.values, 1)
    trend = np.poly1d(coeffs)(x_num)
    ax.plot(rate.index, trend, linestyle="--", linewidth=1.0, color="#555555",
            alpha=0.8, label="Trend")

    ax.set_ylabel("Catch rate (% of visits)")
    ax.set_xlabel("Week starting")
    ax.set_title("Weekly catch rate (catches as % of visits)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m/%Y"))
    ax.legend(fontsize=8)
    fig.autofmt_xdate()
    return _fig_to_buf(fig)


def plot_species_over_time(df: pd.DataFrame) -> io.BytesIO:
    catches = df[df["strikes"] > 0].copy()
    catches["species caught"] = catches["species caught"].replace({"None": pd.NA, "": pd.NA})
    catches = catches.dropna(subset=["species caught"])

    fig, ax = plt.subplots()
    if catches.empty:
        ax.text(0.5, 0.5, "No catches recorded", ha="center", va="center")
        ax.set_axis_off()
    else:
        for species in sorted(catches["species caught"].unique()):
            weekly = (
                catches[catches["species caught"] == species]
                .set_index("date")["strikes"]
                .resample("W-MON", label="left", closed="left")
                .sum()
            )
            ax.plot(weekly.index, weekly.values, marker="o", label=species)
        ax.set_ylabel("Catches")
        ax.set_xlabel("Week starting")
        ax.set_title("Catches per week by species")
        ax.legend()
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m/%Y"))
        fig.autofmt_xdate()
    return _fig_to_buf(fig)


def inter_catch_fig_height(n: int) -> float:
    return max(PLOT_H_IN, 0.35 * n + 1.0)


def plot_inter_catch_interval(df: pd.DataFrame, by_trap: pd.DataFrame, top_n: int) -> io.BytesIO:
    n = min(top_n, len(by_trap))
    top_codes = by_trap.nlargest(n, "rate").index.tolist()

    intervals: dict[str, list[float]] = {}
    for code in top_codes:
        catches = (
            df[(df["code"] == code) & (df["strikes"] > 0)]
            .sort_values("date")["date"]
        )
        if len(catches) >= 2:
            diffs = catches.diff().dropna().dt.days.tolist()
            intervals[code] = diffs

    fig_h = inter_catch_fig_height(len(intervals))
    fig, ax = plt.subplots(figsize=(PLOT_W_IN, fig_h))

    if not intervals:
        ax.text(0.5, 0.5, "Insufficient repeat-catch data for interval analysis",
                ha="center", va="center")
        ax.set_axis_off()
    else:
        sorted_codes = sorted(intervals, key=lambda c: float(np.median(intervals[c])))
        ax.boxplot(
            [intervals[c] for c in sorted_codes],
            tick_labels=sorted_codes,
            vert=False,
            flierprops=dict(marker="o", markersize=3),
        )
        ax.set_xlabel("Days between consecutive catches")
        ax.set_title(f"Inter-catch interval — top {len(intervals)} traps by catch rate")

    return _fig_to_buf(fig)


def plot_catch_concentration(df: pd.DataFrame) -> io.BytesIO:
    fig, ax = plt.subplots()
    trap_types = sorted(df["trap type"].dropna().unique())
    has_data = False
    for trap_type in trap_types:
        by_trap = (
            df[df["trap type"] == trap_type]
            .groupby("code")["strikes"]
            .sum()
            .sort_values(ascending=False)
        )
        if by_trap.sum() == 0:
            continue
        has_data = True
        x = np.arange(1, len(by_trap) + 1) / len(by_trap) * 100
        y = by_trap.cumsum() / by_trap.sum() * 100
        ax.plot(x, y.values, label=trap_type)

    if not has_data:
        ax.text(0.5, 0.5, "No catches recorded", ha="center", va="center")
        ax.set_axis_off()
    else:
        ax.axhline(80, linestyle="--", linewidth=0.75, color="#555555",
                   alpha=0.7, label="80%")
        ax.set_xlabel("% of traps (ranked by catches, highest first)")
        ax.set_ylabel("Cumulative % of catches")
        ax.set_title("Cumulative catch distribution by trap type")
        ax.set_xlim(0, 100)
        ax.set_ylim(0, 100)
        ax.legend(fontsize=8)
    return _fig_to_buf(fig)


def plot_catch_rates(by_trap: pd.DataFrame, top_n: int) -> io.BytesIO:
    fig, ax = plt.subplots()

    if by_trap.empty:
        ax.text(0.5, 0.5, f"No traps with ≥{CATCH_RATE_MIN_VISITS} visits",
                ha="center", va="center")
        ax.set_axis_off()
    else:
        n = min(top_n, len(by_trap))
        best = by_trap.nlargest(n, "rate").sort_values("rate")
        ax.barh(best.index, best["rate"], color="#3b6ea2")
        for i, (rate, visits) in enumerate(zip(best["rate"], best["visits"])):
            ax.text(rate, i, f" {rate:.1f}% ({int(visits)}v)", va="center", fontsize=8)
        ax.set_xlabel("Catch rate (%)")
        ax.set_title(f"Top {n} traps by catch rate (min. {CATCH_RATE_MIN_VISITS} visits)")

    return _fig_to_buf(fig)




def plot_sprung_no_catch(df: pd.DataFrame, top_n: int) -> io.BytesIO:
    visits = df.groupby("code").size().rename("visits")
    sprung_count = (
        df[(df["status"] == "Sprung") & (df["strikes"] == 0)]
        .groupby("code")
        .size()
        .rename("sprung")
    )
    by_trap = pd.concat([visits, sprung_count], axis=1).fillna(0)
    by_trap["rate"] = by_trap["sprung"] / by_trap["visits"] * 100
    top = by_trap.nlargest(top_n, "rate")

    fig, ax = plt.subplots()
    if top.empty or top["sprung"].sum() == 0:
        ax.text(0.5, 0.5, "No sprung-with-no-catch visits recorded",
                ha="center", va="center")
        ax.set_axis_off()
    else:
        s = top.sort_values("rate")
        ax.barh(s.index, s["rate"], color="#b07d3b")
        for i, (rate, sprung, visits) in enumerate(
            zip(s["rate"], s["sprung"], s["visits"])
        ):
            ax.text(rate, i, f" {rate:.1f}% ({int(sprung)}/{int(visits)})",
                    va="center", fontsize=8)
        ax.set_xlabel("% of visits found sprung with no catch")
        ax.set_title(f"Top {len(s)} traps most often sprung with no catch")
    return _fig_to_buf(fig)


def plot_bait_missing(df: pd.DataFrame, top_n: int) -> io.BytesIO:
    visits = df.groupby("code").size().rename("visits")
    bait_missing_count = (
        df[df["status"] == "Still set, bait missing"]
        .groupby("code")
        .size()
        .rename("bait_missing")
    )
    by_trap = pd.concat([visits, bait_missing_count], axis=1).fillna(0)
    by_trap["rate"] = by_trap["bait_missing"] / by_trap["visits"] * 100
    top = by_trap.nlargest(top_n, "rate")

    fig, ax = plt.subplots()
    if top.empty or top["bait_missing"].sum() == 0:
        ax.text(0.5, 0.5, "No bait-missing visits recorded",
                ha="center", va="center")
        ax.set_axis_off()
    else:
        s = top.sort_values("rate")
        ax.barh(s.index, s["rate"], color="#5a7a3b")
        for i, (rate, bm, visits) in enumerate(
            zip(s["rate"], s["bait_missing"], s["visits"])
        ):
            ax.text(rate, i, f" {rate:.1f}% ({int(bm)}/{int(visits)})",
                    va="center", fontsize=8)
        ax.set_xlabel("% of visits found with bait missing")
        ax.set_title(f"Top {len(s)} traps most often found with bait missing")
    return _fig_to_buf(fig)


def plot_status(status: pd.Series) -> io.BytesIO:
    fig, ax = plt.subplots()
    s = status.sort_values()
    ax.barh(s.index, s.values, color="#666666")
    for i, v in enumerate(s.values):
        ax.text(v, i, f" {v}", va="center")
    ax.set_xlabel("Visits")
    ax.set_title("Trap status distribution")
    return _fig_to_buf(fig)


def plot_catch_by_interval(df: pd.DataFrame) -> io.BytesIO:
    """Catch rate and per-day yield versus the gap since the last check.

    For every visit we measure how many days have passed since that trap was
    previously checked, then bin those gaps. Two metrics are plotted together
    because they tell opposite halves of the same story:

    * Catch rate (bars) — the share of visits that found a catch. This rises
      with the gap simply because a longer-unwatched trap has had more time to
      catch something, so a visit is more likely to find a result.
    * Catches per trap-day (line) — catches divided by the total trap-days in
      the bin, i.e. how fast the network is actually catching. This *falls* as
      gaps grow: a caught animal sits in a sprung trap doing nothing until
      someone resets it, and bait degrades.

    Together they frame the checking-frequency trade-off: check often for
    responsiveness and throughput, or check rarely for less effort per catch
    found. A trap's first-ever visit (no prior check) and same-day re-checks
    (interval 0) are excluded.
    """
    d = df.sort_values(["code", "date"]).copy()
    d["interval"] = d.groupby("code")["date"].diff().dt.days
    d = d[d["interval"] > 0]

    fig, ax = plt.subplots()
    if d.empty:
        ax.text(0.5, 0.5, "Insufficient repeat-visit data", ha="center", va="center")
        ax.set_axis_off()
        return _fig_to_buf(fig)

    d["bin"] = pd.cut(d["interval"], bins=INTERVAL_BINS, labels=INTERVAL_LABELS)
    g = d.groupby("bin", observed=True).agg(
        visits=("strikes", "size"),
        catches=("strikes", "sum"),
        trap_days=("interval", "sum"),
    )
    g = g[g["visits"] > 0]
    g["rate"] = g["catches"] / g["visits"] * 100
    g["per_day"] = g["catches"] / g["trap_days"]

    x = np.arange(len(g))
    bars = ax.bar(x, g["rate"], color="#3b6ea2", label="Catch rate (% of visits)")
    for xi, (rate, visits) in enumerate(zip(g["rate"], g["visits"])):
        ax.text(xi, rate, f"{rate:.0f}%\n({int(visits)}v)",
                ha="center", va="bottom", fontsize=7)
    ax.set_xticks(x)
    ax.set_xticklabels(g.index)
    ax.set_xlabel("Days since previous check")
    ax.set_ylabel("Catch rate (% of visits)", color="#3b6ea2")
    ax.set_ylim(0, g["rate"].max() * 1.25)

    ax2 = ax.twinx()
    ax2.grid(False)  # keep the twin axis from double-drawing the seaborn grid
    line, = ax2.plot(x, g["per_day"], marker="o", color="#a23b3b",
                     label="Catches per trap-day")
    ax2.set_ylabel("Catches per trap-day", color="#a23b3b")
    ax2.set_ylim(0, g["per_day"].max() * 1.25)

    ax.set_title("Catch rate and yield vs checking interval")
    ax.legend([bars, line], [bars.get_label(), line.get_label()],
              fontsize=8, loc="upper left")
    return _fig_to_buf(fig)


def plot_spatial_autocorrelation(df: pd.DataFrame, by_trap: pd.DataFrame) -> io.BytesIO:
    """Moran scatterplot: do neighbouring traps have similar catch rates?

    Asks whether catches cluster in space — are good traps surrounded by good
    traps (suggesting local pest density drives results) or scattered at random
    (suggesting per-trap luck or micro-siting)? Each trap with enough visits is
    paired with its SPATIAL_KNN nearest neighbours (Euclidean distance on NZTM
    easting/northing, already in metres). We standardise every trap's catch rate
    and plot it (x) against the mean of its neighbours' standardised rates (y).

    The slope of that cloud *is* Moran's I, the standard measure of spatial
    autocorrelation: I > 0 means clustering (the upper-right and lower-left
    quadrants fill — hot near hot, cold near cold), I ~ 0 means no spatial
    pattern, I < 0 means a checkerboard. A permutation test (shuffling rates
    across locations SPATIAL_PERMUTATIONS times) gives a p-value for whether the
    observed clustering could have arisen by chance.
    """
    fig, ax = plt.subplots()

    coords = df.groupby("code")[["easting", "northing"]].first().apply(
        pd.to_numeric, errors="coerce"
    )
    sub = by_trap.join(coords, how="inner").dropna(subset=["easting", "northing"])

    if len(sub) < SPATIAL_MIN_TRAPS:
        ax.text(0.5, 0.5,
                f"Need ≥{SPATIAL_MIN_TRAPS} traps with coordinates and "
                f"≥{CATCH_RATE_MIN_VISITS} visits", ha="center", va="center")
        ax.set_axis_off()
        return _fig_to_buf(fig)

    rate = sub["rate"].to_numpy(float)
    xy = sub[["easting", "northing"]].to_numpy(float)
    n = len(rate)
    k = min(SPATIAL_KNN, n - 1)

    # row-standardised k-nearest-neighbour weight matrix
    dist2 = ((xy[:, None, :] - xy[None, :, :]) ** 2).sum(-1)
    np.fill_diagonal(dist2, np.inf)
    nn = np.argsort(dist2, axis=1)[:, :k]
    weights = np.zeros((n, n))
    weights[np.repeat(np.arange(n), k), nn.ravel()] = 1.0 / k

    dev = rate - rate.mean()
    denom = (dev ** 2).sum()
    if denom == 0:
        ax.text(0.5, 0.5, "No variation in catch rate to correlate",
                ha="center", va="center")
        ax.set_axis_off()
        return _fig_to_buf(fig)

    moran_i = float(dev @ (weights @ dev) / denom)

    # permutation test: how often does a random reshuffle match this clustering?
    rng = np.random.default_rng(0)
    ge = 1  # count the observed statistic itself
    for _ in range(SPATIAL_PERMUTATIONS):
        perm = rng.permutation(dev)
        if perm @ (weights @ perm) / denom >= moran_i:
            ge += 1
    p_value = ge / (SPATIAL_PERMUTATIONS + 1)

    z = dev / np.sqrt((dev ** 2).mean())  # standardised catch rate
    lag = weights @ z                      # spatial lag (neighbour mean)

    ax.axhline(0, lw=0.6, color="#999999")
    ax.axvline(0, lw=0.6, color="#999999")
    ax.scatter(z, lag, s=18, color="#3b6ea2", alpha=0.8)
    xs = np.array([z.min(), z.max()])
    ax.plot(xs, moran_i * xs, "--", lw=1.0, color="#a23b3b")
    ax.set_xlabel("Trap catch rate (standardised)")
    ax.set_ylabel(f"Mean of {k} nearest neighbours (standardised)")
    ax.set_title(
        f"Spatial clustering of catch rate — Moran's I = {moran_i:.2f} "
        f"(p = {p_value:.3f})"
    )
    return _fig_to_buf(fig)


def underperformer_fig_height(n: int) -> float:
    return max(PLOT_H_IN, 0.32 * n + 1.2)


def plot_underperformers(under: pd.DataFrame, top_n: int) -> io.BytesIO:
    """Dumbbell chart of traps lagging nearby traps of their own type.

    Each row is a flagged trap: a red dot for its own catch rate, a blue dot for
    the mean of its same-type neighbours, and a bar joining them whose length is
    the shortfall (annotated in percentage points). The worst offenders sit at
    the top. `under` is the pre-computed, already-sorted frame from
    compute_underperformers; we show at most `top_n` of them.
    """
    n = min(top_n, len(under))
    fig_h = underperformer_fig_height(n) if n else PLOT_H_IN
    fig, ax = plt.subplots(figsize=(PLOT_W_IN, fig_h))

    if under.empty:
        ax.text(0.5, 0.5, "No traps lagging nearby traps of their type",
                ha="center", va="center")
        ax.set_axis_off()
        return _fig_to_buf(fig)

    # nlargest gives the worst gaps; sort ascending so the worst ends up on top
    top = under.nlargest(n, "gap").sort_values("gap")
    y = np.arange(len(top))
    ax.hlines(y, top["rate"], top["neighbour_mean"], color="#cccccc", lw=1.5, zorder=1)
    ax.scatter(top["rate"], y, color="#a23b3b", s=28, zorder=2, label="This trap")
    ax.scatter(top["neighbour_mean"], y, color="#3b6ea2", s=28, zorder=2,
               label="Nearby same-type mean")
    for yi, (nm, gap) in enumerate(zip(top["neighbour_mean"], top["gap"])):
        ax.text(nm, yi, f"  −{gap:.0f}pp", va="center", fontsize=7, color="#555555")

    ax.set_yticks(y)
    ax.set_yticklabels(top.index)
    ax.set_xlim(0, top["neighbour_mean"].max() * 1.18)
    ax.set_xlabel("Catch rate (% of visits)")
    ax.set_title(f"Traps underperforming nearby traps of the same type (worst {len(top)})")
    ax.legend(fontsize=8, loc="lower right")
    return _fig_to_buf(fig)


ALL_ANALYSES = {"species", "bait", "bait_traptype", "species_bait", "over_time", "rate_over_time", "species_over_time", "cumulative", "catch_rates", "catch_concentration", "inter_catch", "interval", "spatial", "underperformers", "sprung", "bait_missing", "status"}


def make_plots(df: pd.DataFrame, stats: dict, selected: set[str], top_n: int) -> dict:
    builders = {
        "species":           lambda: plot_species(stats["species"]),
        "bait":              lambda: plot_catch_by_bait(stats["by_bait"]),
        "bait_traptype":     lambda: plot_catch_by_bait_traptype(df, stats["by_bait"]),
        "species_bait":      lambda: plot_species_bait_heatmap(df, stats["by_bait"]),
        "over_time":         lambda: plot_over_time(df),
        "rate_over_time":    lambda: plot_catch_rate_over_time(df),
        "species_over_time": lambda: plot_species_over_time(df),
        "cumulative":        lambda: plot_cumulative_catches(df),
        "catch_rates":       lambda: plot_catch_rates(stats["by_trap_rate"], top_n),
        "catch_concentration":   lambda: plot_catch_concentration(df),
        "inter_catch":       lambda: plot_inter_catch_interval(df, stats["by_trap_rate"], top_n),
        "interval":          lambda: plot_catch_by_interval(df),
        "spatial":           lambda: plot_spatial_autocorrelation(df, stats["by_trap_rate"]),
        "underperformers":   lambda: plot_underperformers(stats["underperformers"], top_n),
        "sprung":            lambda: plot_sprung_no_catch(df, top_n),
        "bait_missing":      lambda: plot_bait_missing(df, top_n),
        "status":            lambda: plot_status(stats["status"]),
    }
    return {key: fn() for key, fn in builders.items() if key in selected}


# ---------- pdf ----------


def _img(buf: io.BytesIO, height: float = PLOT_H_IN) -> Image:
    return Image(buf, width=PLOT_W_IN * inch, height=height * inch)


def _kv_table(rows: list[tuple[str, str]]) -> Table:
    t = Table(rows, hAlign="LEFT", colWidths=[2.2 * inch, 3.0 * inch])
    t.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "Helvetica", 10),
                ("FONT", (0, 0), (0, -1), "Helvetica-Bold", 10),
                ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return t


def _grid_table(header: list[str], rows: list[list[str]]) -> Table:
    data = [header] + rows
    t = Table(data, hAlign="LEFT")
    t.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "Helvetica", 9),
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
            ]
        )
    )
    return t


def build_pdf(stats: dict, plots: dict, source_name: str, out_path: Path, top_n: int) -> None:
    styles = getSampleStyleSheet()
    h1 = styles["Heading1"]
    h2 = styles["Heading2"]
    body = styles["BodyText"]
    small = ParagraphStyle("small", parent=body, fontSize=8, textColor=colors.grey)

    story = []

    date_min = stats["date_min"].strftime("%d/%m/%Y")
    date_max = stats["date_max"].strftime("%d/%m/%Y")

    story.append(Paragraph(f"Trap report: {source_name}", h1))
    story.append(
        Paragraph(
            f"Visits between {date_min} and {date_max}. "
            f"Generated {datetime.now().strftime('%d/%m/%Y %H:%M')}.",
            small,
        )
    )
    story.append(Spacer(1, 0.2 * inch))

    lines_txt = ", ".join(stats["lines"]) if stats["lines"] else "—"
    if stats["unassigned_traps"]:
        lines_txt += f" (+{stats['unassigned_traps']} unassigned)"

    story.append(Paragraph("Summary", h2))
    story.append(
        _kv_table(
            [
                ("Total visits", f"{stats['total_visits']}"),
                ("Unique traps", f"{stats['unique_traps']}"),
                ("Total catches", f"{stats['total_catches']}"),
                ("Overall catch rate", f"{stats['rate_pct']:.1f}% of visits"),
                ("Date range", f"{date_min} — {date_max}"),
                ("Lines present", Paragraph(lines_txt, body)),
            ]
        )
    )
    story.append(Spacer(1, 0.25 * inch))

    sections = [
        ("species",           "Catches by species",            True),
        ("bait",              "Catches by bait",                True),
        ("bait_traptype",     "Catches by bait and trap type",  True),
        ("species_bait",      "Catches by bait and species",    True),
        ("over_time",         "Catches over time",              True),
        ("rate_over_time",    "Catch rate over time",           True),
        ("species_over_time", "Catches over time by species",   True),
        ("cumulative",        "Cumulative catches by species",  True),
        ("catch_rates",       "Trap catch rates",               True),
        ("catch_concentration",   "Catch concentration",            True),
        ("spatial",           "Spatial clustering of catch rates", True),
        ("underperformers",   "Underperforming traps",         True),
        ("inter_catch",       "Inter-catch interval",          True),
        ("interval",          "Catch rate vs checking interval", True),
        ("sprung",            "Frequently sprung traps",       True),
        ("bait_missing",      "Frequently bait-missing traps", True),
        ("status",            "Trap status",                   False),
    ]

    for key, heading, page_break_after in sections:
        if key not in plots:
            continue
        story.append(Paragraph(heading, h2))
        if key == "inter_catch":
            n_boxes = min(top_n, len(stats["by_trap_rate"]))
            story.append(_img(plots[key], height=inter_catch_fig_height(n_boxes)))
        elif key == "underperformers":
            n_rows = min(top_n, len(stats["underperformers"]))
            story.append(_img(plots[key], height=underperformer_fig_height(n_rows)))
        else:
            story.append(_img(plots[key]))

        if key == "species" and not stats["species"].empty:
            rows = [[sp, str(int(n))] for sp, n in stats["species"].items()]
            story.append(Spacer(1, 0.1 * inch))
            story.append(_grid_table(["Species", "Catches"], rows))

        if key == "bait":
            bb = stats["by_bait"]
            if not bb.empty:
                ordered = bb.sort_values("rate", ascending=False)
                story.append(Spacer(1, 0.1 * inch))
                story.append(_grid_table(
                    ["Bait", "Visits", "Catches", "Rate"],
                    [[b, str(int(r.visits)), str(int(r.catches)), f"{r.rate:.1f}%"]
                     for b, r in ordered.iterrows()],
                ))
            story.append(Spacer(1, 0.1 * inch))
            story.append(Paragraph(
                "Catch rate is the percentage of visits carrying a given bait "
                "that recorded a catch, using the bait noted on each visit. "
                "Rate (rather than raw catch count) is shown so heavily-used and "
                "rarely-used baits can be compared fairly. Where a visit lists "
                "more than one bait, each ingredient is counted separately and "
                "shares the credit for any catch — the data can't say which bait "
                f"did the work. Baits recorded on fewer than {BAIT_MIN_VISITS} "
                "visits are omitted as too sparse to rank.",
                small,
            ))

        if key == "bait_traptype":
            story.append(Spacer(1, 0.1 * inch))
            story.append(Paragraph(
                "The same baits as above, now showing the total number of "
                "catches each produced (not the rate), with every bar split by "
                "the type of trap that made the catch. This shows where a bait's "
                "catches actually come from — a bait may pair naturally with one "
                "trap type and rarely be used on others. Bait that appears on a "
                "visit alongside others is counted for each, as before.",
                small,
            ))

        if key == "species_bait":
            story.append(Spacer(1, 0.1 * inch))
            story.append(Paragraph(
                "Each cell is the number of a given species caught over visits "
                "carrying a given bait; darker cells and larger numbers mean more "
                "catches. Reading across a row shows what a bait tends to catch; "
                "reading down a column shows which baits account for a species. "
                "Only catches with an identified species are shown, for the same "
                "baits as the charts above.",
                small,
            ))

        if key == "inter_catch":
            story.append(Spacer(1, 0.1 * inch))
            story.append(Paragraph(
                "Each box shows the distribution of days between consecutive catches "
                "for that trap, based on the top-performing traps by catch rate. "
                "The box spans the interquartile range (25th to 75th percentile), "
                "the line inside is the median, and the whiskers extend to the most "
                "extreme values within 1.5× the interquartile range. Points beyond "
                "the whiskers are outliers. Traps are ordered by median interval, "
                "with the most frequently catching traps at the top.",
                small,
            ))

        if key == "interval":
            story.append(Spacer(1, 0.1 * inch))
            story.append(Paragraph(
                "How the gap since a trap was last checked relates to what the "
                "next visit finds. The bars (left axis) show the catch rate — the "
                "share of visits that found a catch — which climbs with the gap "
                "because a longer-unwatched trap has had more time to catch "
                "something. The line (right axis) shows catches per trap-day, "
                "i.e. how fast the network actually catches; it falls as gaps "
                "grow, because a sprung trap sits idle and bait degrades until "
                "someone returns. Checking often favours throughput and "
                "responsiveness; checking rarely means less effort per catch "
                "found. Each trap's first visit and same-day re-checks are "
                "excluded.",
                small,
            ))

        if key == "spatial":
            story.append(Spacer(1, 0.1 * inch))
            story.append(Paragraph(
                "Whether catch rates cluster geographically. Each trap's "
                "standardised catch rate (horizontal) is plotted against the mean "
                "of its nearest neighbours' standardised rates (vertical); the "
                "slope of the cloud is Moran's I. A positive I (points trending "
                "into the upper-right and lower-left quadrants) means good traps "
                "sit near good traps and quiet traps near quiet ones — catches "
                "track local conditions rather than landing at random. A value "
                "near zero means no spatial pattern. The p-value comes from "
                "reshuffling catch rates across locations many times: a small "
                "p-value means clustering this strong is unlikely by chance. Only "
                f"traps with at least {CATCH_RATE_MIN_VISITS} visits are included.",
                small,
            ))

        if key == "underperformers":
            up = stats["underperformers"]
            if not up.empty:
                shown = up.nlargest(min(top_n, len(up)), "gap")
                story.append(Spacer(1, 0.1 * inch))
                story.append(_grid_table(
                    ["Trap", "Type", "Visits", "Catch rate", "Nearby mean", "Shortfall"],
                    [[code, r["trap type"], str(int(r.visits)),
                      f"{r.rate:.1f}%", f"{r.neighbour_mean:.1f}%", f"{r.gap:.1f} pp"]
                     for code, r in shown.iterrows()],
                ))
            story.append(Spacer(1, 0.1 * inch))
            story.append(Paragraph(
                "A watchlist of traps catching less than comparable traps nearby. "
                "Each trap is compared only to its nearest neighbours of the same "
                "trap type, so a low rate that simply reflects the trap type "
                "(rather than a problem with this trap) is not flagged. Listed "
                f"traps sit at least {UNDERPERFORMER_MIN_GAP:.0f} percentage "
                "points below that same-type neighbour mean — candidates for "
                "checking the mechanism, bait, or siting, or for moving to a "
                "busier spot. A large shortfall next to a high neighbour mean is "
                "the strongest signal. Based on traps with at least "
                f"{CATCH_RATE_MIN_VISITS} visits; the gap is in percentage points.",
                small,
            ))

        if key == "catch_concentration":
            story.append(Spacer(1, 0.1 * inch))
            story.append(Paragraph(
                "Each curve shows what percentage of total catches for a trap type are "
                "accounted for by a given percentage of traps of that type, with traps ranked "
                "from highest to lowest catching. A curve that rises steeply indicates that "
                "catches are concentrated in a small number of traps; a curve closer to "
                "the diagonal indicates catches are spread more evenly across the network. "
                "The dashed line marks the 80% level — where a curve crosses this line "
                "shows what proportion of traps accounts for 80% of catches for that trap type.",
                small,
            ))

        if key == "catch_rates":
            bt = stats["by_trap_rate"]
            if not bt.empty:
                n = min(top_n, len(bt))
                best = bt.nlargest(n, "rate").sort_values("rate", ascending=False)
                story.append(Spacer(1, 0.1 * inch))
                story.append(_grid_table(
                    ["Trap", "Visits", "Catches", "Rate"],
                    [[t, str(int(r.visits)), str(int(r.catches)), f"{r.rate:.1f}%"]
                     for t, r in best.iterrows()],
                ))

        if page_break_after:
            story.append(PageBreak())

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=0.7 * inch,
        rightMargin=0.7 * inch,
        topMargin=0.6 * inch,
        bottomMargin=0.7 * inch,
        title=f"Trap report — {source_name}",
    )
    def _add_page_number(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.grey)
        canvas.drawCentredString(A4[0] / 2, 0.35 * inch, f"Page {doc.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_add_page_number, onLaterPages=_add_page_number)


# ---------- pipeline ----------


def process(csv_path: Path, selected: set[str], top_n: int, line: str | None = None) -> Path:
    df = load_visits(csv_path)
    df = resolve_lines(df, csv_path.parent / LINE_ASSIGNMENTS_CSV)

    if line is not None:
        available = sorted(df["line"].dropna().unique().tolist())
        if line not in available:
            raise LineNotFound(
                f"line {line!r} not present in {csv_path.name}; "
                f"available: {', '.join(available) or 'none'}"
            )
        df = df[df["line"] == line].copy()
        safe = re.sub(r"\W+", "_", line).strip("_")
        out_path = csv_path.with_name(f"{csv_path.stem}_{safe}_report.pdf")
        source_name = f"{csv_path.name} — line {line}"
    else:
        out_path = csv_path.with_name(f"{csv_path.stem}_report.pdf")
        source_name = csv_path.name

    stats = compute_stats(df)
    plots = make_plots(df, stats, selected, top_n)
    build_pdf(stats, plots, source_name, out_path, top_n)
    return out_path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("csv", nargs="?", type=Path, help="CSV file to report on")
    p.add_argument(
        "-a",
        "--all",
        action="store_true",
        help="Process every *.csv in the current directory",
    )

    analysis_group = p.add_argument_group(
        "analyses",
        "All analyses are included by default; use --no-X to exclude one",
    )
    analysis_group.add_argument(
        "--no-species", action="store_true", help="Exclude catches by species"
    )
    analysis_group.add_argument(
        "--no-bait", action="store_true", help="Exclude catch rate by bait type"
    )
    analysis_group.add_argument(
        "--no-bait-traptype", action="store_true",
        help="Exclude catches per bait stacked by trap type"
    )
    analysis_group.add_argument(
        "--no-species-bait", action="store_true",
        help="Exclude bait-by-species catch-count heatmap"
    )
    analysis_group.add_argument(
        "--no-over-time", action="store_true", help="Exclude catches per week over time"
    )
    analysis_group.add_argument(
        "--no-rate-over-time", action="store_true",
        help="Exclude weekly catch rate (catches as %% of visits) over time"
    )
    analysis_group.add_argument(
        "--no-species-over-time", action="store_true",
        help="Exclude catches per week broken down by species"
    )
    analysis_group.add_argument(
        "--no-cumulative", action="store_true",
        help="Exclude cumulative catches over time by species"
    )
    analysis_group.add_argument(
        "--no-catch-rates", action="store_true",
        help=f"Exclude top-N traps by catch rate (min. {CATCH_RATE_MIN_VISITS} visits)"
    )
    analysis_group.add_argument(
        "--no-catch-concentration", action="store_true",
        help="Exclude Pareto curves by species: %% of traps vs cumulative %% of catches"
    )
    analysis_group.add_argument(
        "--no-inter-catch", action="store_true",
        help="Exclude box plot of days between catches for top-N traps by catch rate"
    )
    analysis_group.add_argument(
        "--no-interval", action="store_true",
        help="Exclude catch rate and per-day yield vs checking interval"
    )
    analysis_group.add_argument(
        "--no-spatial", action="store_true",
        help="Exclude spatial clustering (Moran's I) of trap catch rates"
    )
    analysis_group.add_argument(
        "--no-underperformers", action="store_true",
        help="Exclude watchlist of traps lagging nearby same-type traps"
    )
    analysis_group.add_argument(
        "--no-sprung", action="store_true",
        help="Exclude most frequently sprung traps with no catch"
    )
    analysis_group.add_argument(
        "--no-bait-missing", action="store_true",
        help="Exclude most frequently bait-missing traps"
    )
    analysis_group.add_argument(
        "--no-status", action="store_true", help="Exclude trap status distribution"
    )
    p.add_argument(
        "--line", type=str, default=None, metavar="LINE",
        help="Limit the analysis to a single line (by name)"
    )
    p.add_argument(
        "--top-n", type=int, default=DEFAULT_TOP_N, metavar="N",
        help=f"Number of top traps to show in catch-rate and sprung analyses (default: {DEFAULT_TOP_N})"
    )

    args = p.parse_args(argv)

    if args.all and args.csv is not None:
        p.error("give a CSV path OR -a, not both")
    if not args.all and args.csv is None:
        p.error("give a CSV path or use -a")

    excluded = {
        key
        for key, flag in [
            ("species",             args.no_species),
            ("bait",                args.no_bait),
            ("bait_traptype",       args.no_bait_traptype),
            ("species_bait",        args.no_species_bait),
            ("over_time",           args.no_over_time),
            ("rate_over_time",      args.no_rate_over_time),
            ("species_over_time",   args.no_species_over_time),
            ("cumulative",          args.no_cumulative),
            ("catch_rates",         args.no_catch_rates),
            ("catch_concentration", args.no_catch_concentration),
            ("inter_catch",         args.no_inter_catch),
            ("interval",            args.no_interval),
            ("spatial",             args.no_spatial),
            ("underperformers",     args.no_underperformers),
            ("sprung",              args.no_sprung),
            ("bait_missing",        args.no_bait_missing),
            ("status",              args.no_status),
        ]
        if flag
    }
    selected = ALL_ANALYSES - excluded

    if args.all:
        # Skip the trap-line assignments file: it's a lookup table consumed by
        # resolve_lines, not a visit log to report on.
        paths = [
            p for p in sorted(Path(".").glob("*.csv"))
            if p.name != LINE_ASSIGNMENTS_CSV
        ]
        if not paths:
            print("No CSV files found in current directory.", file=sys.stderr)
            return 1
    else:
        if not args.csv.is_file():
            print(f"Not a file: {args.csv}", file=sys.stderr)
            return 1
        paths = [args.csv]

    for path in paths:
        try:
            out = process(path, selected, args.top_n, args.line)
        except LineNotFound as e:
            if args.all:
                print(f"Skipping {path}: {e}", file=sys.stderr)
                continue
            print(str(e), file=sys.stderr)
            return 1
        except Exception as e:
            print(f"FAILED {path}: {e}", file=sys.stderr)
            return 2
        print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

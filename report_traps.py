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

Usage:
    report_traps.py <csv>                       # all analyses -> <stem>_report.pdf
    report_traps.py -a                          # every *.csv in cwd
    report_traps.py <csv> --species             # single analysis
    report_traps.py <csv> --catch-rates --top-n 10  # top 10 by catch rate

Analysis flags (combine freely; omit all to include everything):
    --species              Catches by species
    --over-time            Catches per week over time (with linear trend)
    --rate-over-time       Weekly catch rate (% of visits) over time
    --species-over-time    Catches per week broken down by species
    --cumulative           Cumulative catches over time by species
    --catch-rates          Top-N traps by catch rate (min. 3 visits)
    --catch-concentration  Pareto curves by species: % of traps vs cumulative % of catches
    --inter-catch          Inter-catch interval box plot for top-N traps
    --sprung               Top-N traps most often found sprung with no catch
    --status               Trap status distribution

Other options:
    --top-n N              Number of traps shown in catch-rates, inter-catch,
                           and sprung analyses (default: 20)
    -a, --all              Process every *.csv in the current directory
"""

import argparse
import glob
import io
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
DEFAULT_TOP_N = 20

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

RAT_ALIASES = {"Rat - Ship", "Rat - Norway"}


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
    }


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
    catches = df[df["strikes"] > 0].copy()
    catches["species caught"] = catches["species caught"].replace({"None": pd.NA, "": pd.NA})
    catches = catches.dropna(subset=["species caught"])

    fig, ax = plt.subplots()
    if catches.empty:
        ax.text(0.5, 0.5, "No catches recorded", ha="center", va="center")
        ax.set_axis_off()
    else:
        for species in sorted(catches["species caught"].unique()):
            by_trap = (
                catches[catches["species caught"] == species]
                .groupby("code")["strikes"]
                .sum()
                .sort_values(ascending=False)
            )
            x = np.arange(1, len(by_trap) + 1) / len(by_trap) * 100
            y = by_trap.cumsum() / by_trap.sum() * 100
            ax.plot(x, y.values, label=species)

        ax.axhline(80, linestyle="--", linewidth=0.75, color="#555555",
                   alpha=0.7, label="80%")
        ax.set_xlabel("% of traps (ranked by catches, highest first)")
        ax.set_ylabel("Cumulative % of catches")
        ax.set_title("Cumulative catch distribution by species")
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


def plot_status(status: pd.Series) -> io.BytesIO:
    fig, ax = plt.subplots()
    s = status.sort_values()
    ax.barh(s.index, s.values, color="#666666")
    for i, v in enumerate(s.values):
        ax.text(v, i, f" {v}", va="center")
    ax.set_xlabel("Visits")
    ax.set_title("Trap status distribution")
    return _fig_to_buf(fig)


ALL_ANALYSES = {"species", "over_time", "rate_over_time", "species_over_time", "cumulative", "catch_rates", "catch_concentration", "inter_catch", "sprung", "status"}


def make_plots(df: pd.DataFrame, stats: dict, selected: set[str], top_n: int) -> dict:
    builders = {
        "species":           lambda: plot_species(stats["species"]),
        "over_time":         lambda: plot_over_time(df),
        "rate_over_time":    lambda: plot_catch_rate_over_time(df),
        "species_over_time": lambda: plot_species_over_time(df),
        "cumulative":        lambda: plot_cumulative_catches(df),
        "catch_rates":       lambda: plot_catch_rates(stats["by_trap_rate"], top_n),
        "catch_concentration":   lambda: plot_catch_concentration(df),
        "inter_catch":       lambda: plot_inter_catch_interval(df, stats["by_trap_rate"], top_n),
        "sprung":            lambda: plot_sprung_no_catch(df, top_n),
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

    story.append(Paragraph("Summary", h2))
    story.append(
        _kv_table(
            [
                ("Total visits", f"{stats['total_visits']}"),
                ("Unique traps", f"{stats['unique_traps']}"),
                ("Total catches", f"{stats['total_catches']}"),
                ("Overall catch rate", f"{stats['rate_pct']:.1f}% of visits"),
                ("Date range", f"{date_min} — {date_max}"),
            ]
        )
    )
    story.append(Spacer(1, 0.25 * inch))

    sections = [
        ("species",           "Catches by species",            False),
        ("over_time",         "Catches over time",              True),
        ("rate_over_time",    "Catch rate over time",           True),
        ("species_over_time", "Catches over time by species",   True),
        ("cumulative",        "Cumulative catches by species",  True),
        ("catch_rates",       "Trap catch rates",               True),
        ("catch_concentration",   "Catch concentration",            True),
        ("inter_catch",       "Inter-catch interval",          True),
        ("sprung",            "Frequently sprung traps",       True),
        ("status",            "Trap status",                   False),
    ]

    for key, heading, page_break_after in sections:
        if key not in plots:
            continue
        story.append(Paragraph(heading, h2))
        if key == "inter_catch":
            n_boxes = min(top_n, len(stats["by_trap_rate"]))
            story.append(_img(plots[key], height=inter_catch_fig_height(n_boxes)))
        else:
            story.append(_img(plots[key]))

        if key == "species" and not stats["species"].empty:
            rows = [[sp, str(int(n))] for sp, n in stats["species"].items()]
            story.append(Spacer(1, 0.1 * inch))
            story.append(_grid_table(["Species", "Catches"], rows))

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

        if key == "catch_concentration":
            story.append(Spacer(1, 0.1 * inch))
            story.append(Paragraph(
                "Each curve shows what percentage of total catches for a species are "
                "accounted for by a given percentage of traps, with traps ranked from "
                "highest to lowest catching. A curve that rises steeply indicates that "
                "catches are concentrated in a small number of traps; a curve closer to "
                "the diagonal indicates catches are spread more evenly across the network. "
                "The dashed line marks the 80% level — where a curve crosses this line "
                "shows what proportion of traps accounts for 80% of catches for that species.",
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


def process(csv_path: Path, selected: set[str], top_n: int) -> Path:
    df = load_visits(csv_path)
    stats = compute_stats(df)
    plots = make_plots(df, stats, selected, top_n)
    out_path = csv_path.with_name(f"{csv_path.stem}_report.pdf")
    build_pdf(stats, plots, csv_path.name, out_path, top_n)
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
        "Select specific analyses to include (default: all)",
    )
    analysis_group.add_argument(
        "--species", action="store_true", help="Catches by species"
    )
    analysis_group.add_argument(
        "--over-time", action="store_true", help="Catches per week over time"
    )
    analysis_group.add_argument(
        "--rate-over-time", action="store_true",
        help="Weekly catch rate (catches as % of visits) over time"
    )
    analysis_group.add_argument(
        "--species-over-time", action="store_true", help="Catches per week broken down by species"
    )
    analysis_group.add_argument(
        "--cumulative", action="store_true", help="Cumulative catches over time by species"
    )
    analysis_group.add_argument(
        "--catch-rates", action="store_true",
        help=f"Top-N traps by catch rate (min. {CATCH_RATE_MIN_VISITS} visits)"
    )
    analysis_group.add_argument(
        "--catch-concentration", action="store_true",
        help="Pareto curves by species: % of traps vs cumulative % of catches"
    )
    analysis_group.add_argument(
        "--inter-catch", action="store_true",
        help="Box plot of days between catches for top-N traps by catch rate"
    )
    analysis_group.add_argument(
        "--sprung", action="store_true",
        help="Most frequently sprung traps with no catch"
    )
    p.add_argument(
        "--top-n", type=int, default=DEFAULT_TOP_N, metavar="N",
        help=f"Number of top traps to show in catch-rate and sprung analyses (default: {DEFAULT_TOP_N})"
    )
    analysis_group.add_argument(
        "--status", action="store_true", help="Trap status distribution"
    )

    args = p.parse_args(argv)

    if args.all and args.csv is not None:
        p.error("give a CSV path OR -a, not both")
    if not args.all and args.csv is None:
        p.error("give a CSV path or use -a")

    requested = {
        key
        for key, flag in [
            ("species",           args.species),
            ("over_time",         args.over_time),
            ("rate_over_time",    args.rate_over_time),
            ("species_over_time", args.species_over_time),
            ("cumulative",        args.cumulative),
            ("catch_rates",       args.catch_rates),
            ("catch_concentration",   args.catch_concentration),
            ("inter_catch",       args.inter_catch),
            ("sprung",            args.sprung),
            ("status",            args.status),
        ]
        if flag
    }
    selected = requested if requested else ALL_ANALYSES

    if args.all:
        paths = sorted(Path(".").glob("*.csv"))
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
            out = process(path, selected, args.top_n)
        except Exception as e:
            print(f"FAILED {path}: {e}", file=sys.stderr)
            return 2
        print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

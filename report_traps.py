#!/usr/bin/env python
"""Generate a styled PDF report from a Trap.NZ visit-log CSV.

Usage:
    report_traps.py <csv>                  # all analyses -> <stem>_report.pdf
    report_traps.py -a                     # every *.csv in cwd
    report_traps.py <csv> --species        # only species breakdown
    report_traps.py <csv> --species --over-time  # two analyses

Analysis flags (combine freely; omit all to include everything):
    --species              Catches by species
    --over-time            Catches per week over time
    --species-over-time    Catches per week broken down by species
    --catch-rates          Best traps by catch rate
    --sprung               Most frequently sprung traps with no catch
    --status               Trap status distribution
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
CATCH_RATE_N = 10

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
    ax.plot(weekly.index, weekly.values, marker="o", color="#a23b3b")
    ax.fill_between(weekly.index, weekly.values, alpha=0.15, color="#a23b3b")
    ax.set_ylabel("Catches")
    ax.set_xlabel("Week starting")
    ax.set_title("Catches per week")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m/%Y"))
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


def plot_catch_rates(by_trap: pd.DataFrame) -> io.BytesIO:
    fig, ax = plt.subplots()

    if by_trap.empty:
        ax.text(0.5, 0.5, f"No traps with ≥{CATCH_RATE_MIN_VISITS} visits",
                ha="center", va="center")
        ax.set_axis_off()
    else:
        n = min(CATCH_RATE_N, len(by_trap))
        best = by_trap.nlargest(n, "rate").sort_values("rate")
        ax.barh(best.index, best["rate"], color="#3b6ea2")
        for i, (rate, visits) in enumerate(zip(best["rate"], best["visits"])):
            ax.text(rate, i, f" {rate:.1f}% ({int(visits)}v)", va="center", fontsize=8)
        ax.set_xlabel("Catch rate (%)")
        ax.set_title(f"Top {n} traps by catch rate (min. {CATCH_RATE_MIN_VISITS} visits)")

    return _fig_to_buf(fig)




def plot_sprung_no_catch(df: pd.DataFrame) -> io.BytesIO:
    sprung = (
        df[(df["status"] == "Sprung") & (df["strikes"] == 0)]
        .groupby("code")
        .size()
        .sort_values(ascending=False)
        .head(20)
    )
    fig, ax = plt.subplots()
    if sprung.empty:
        ax.text(0.5, 0.5, "No sprung-with-no-catch visits recorded",
                ha="center", va="center")
        ax.set_axis_off()
    else:
        s = sprung.sort_values()
        ax.barh(s.index, s.values, color="#b07d3b")
        for i, v in enumerate(s.values):
            ax.text(v, i, f" {v}", va="center")
        ax.set_xlabel("Times found sprung with no catch")
        ax.set_title("Most frequently sprung traps (no catch)")
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


ALL_ANALYSES = {"species", "over_time", "species_over_time", "catch_rates", "sprung", "status"}


def make_plots(df: pd.DataFrame, stats: dict, selected: set[str]) -> dict:
    builders = {
        "species":           lambda: plot_species(stats["species"]),
        "over_time":         lambda: plot_over_time(df),
        "species_over_time": lambda: plot_species_over_time(df),
        "catch_rates":       lambda: plot_catch_rates(stats["by_trap_rate"]),
        "sprung":            lambda: plot_sprung_no_catch(df),
        "status":            lambda: plot_status(stats["status"]),
    }
    return {key: fn() for key, fn in builders.items() if key in selected}


# ---------- pdf ----------


def _img(buf: io.BytesIO) -> Image:
    return Image(buf, width=PLOT_W_IN * inch, height=PLOT_H_IN * inch)


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


def build_pdf(stats: dict, plots: dict, source_name: str, out_path: Path) -> None:
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
        ("over_time",         "Catches over time",             True),
        ("species_over_time", "Catches over time by species",  True),
        ("catch_rates",       "Trap catch rates",              True),
        ("sprung",            "Frequently sprung traps",       True),
        ("status",            "Trap status",                   False),
    ]

    for key, heading, page_break_after in sections:
        if key not in plots:
            continue
        story.append(Paragraph(heading, h2))
        story.append(_img(plots[key]))

        if key == "species" and not stats["species"].empty:
            rows = [[sp, str(int(n))] for sp, n in stats["species"].items()]
            story.append(Spacer(1, 0.1 * inch))
            story.append(_grid_table(["Species", "Catches"], rows))

        if key == "catch_rates":
            bt = stats["by_trap_rate"]
            if not bt.empty:
                n = min(CATCH_RATE_N, len(bt))
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
        bottomMargin=0.6 * inch,
        title=f"Trap report — {source_name}",
    )
    doc.build(story)


# ---------- pipeline ----------


def process(csv_path: Path, selected: set[str]) -> Path:
    df = load_visits(csv_path)
    stats = compute_stats(df)
    plots = make_plots(df, stats, selected)
    out_path = csv_path.with_name(f"{csv_path.stem}_report.pdf")
    build_pdf(stats, plots, csv_path.name, out_path)
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
        "--species-over-time", action="store_true", help="Catches per week broken down by species"
    )
    analysis_group.add_argument(
        "--catch-rates", action="store_true",
        help=f"Best traps by catch rate (min. {CATCH_RATE_MIN_VISITS} visits)"
    )
    analysis_group.add_argument(
        "--sprung", action="store_true",
        help="Most frequently sprung traps with no catch"
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
            ("species_over_time", args.species_over_time),
            ("catch_rates",       args.catch_rates),
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
            out = process(path, selected)
        except Exception as e:
            print(f"FAILED {path}: {e}", file=sys.stderr)
            return 2
        print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

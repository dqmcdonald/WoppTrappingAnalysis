#!/usr/bin/env python
"""Generate a styled PDF report from a Trap.NZ visit-log CSV.

Usage:
    report_traps.py <csv>     # one CSV -> <stem>_report.pdf alongside it
    report_traps.py -a        # every *.csv in cwd
"""

import argparse
import glob
import io
import sys
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
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

plt.rcParams.update({
    "figure.figsize": (PLOT_W_IN, PLOT_H_IN),
    "figure.dpi": PLOT_DPI,
    "font.size": 10,
    "axes.titlesize": 12,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "grid.alpha": 0.25,
})


# ---------- data ----------

def load_visits(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["date"])
    df["strikes"] = pd.to_numeric(df["strikes"], errors="coerce").fillna(0).astype(int)
    for col in ["code", "trap type", "status", "species caught", "line"]:
        if col in df.columns:
            df[col] = df[col].astype("string").str.strip()
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
    by_type = df.groupby("trap type").agg(
        visits=("strikes", "size"),
        catches=("strikes", "sum"),
    )
    by_type["rate_pct"] = by_type["catches"] / by_type["visits"] * 100
    by_type = by_type.sort_values("catches", ascending=False)

    top_traps = (
        df.groupby("code")["strikes"].sum()
        .sort_values(ascending=False)
        .head(10)
    )
    top_traps = top_traps[top_traps > 0]

    status_counts = df["status"].value_counts()

    return {
        "total_visits": total_visits,
        "unique_traps": int(df["trap nid"].nunique()),
        "total_catches": total_catches,
        "rate_pct": rate,
        "date_min": df["date"].min(),
        "date_max": df["date"].max(),
        "species": species,
        "by_type": by_type,
        "top_traps": top_traps,
        "status": status_counts,
    }


# ---------- plots ----------

def _fig_to_buf(fig) -> io.BytesIO:
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", dpi=PLOT_DPI)
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


def plot_by_type(by_type: pd.DataFrame) -> io.BytesIO:
    fig, ax1 = plt.subplots()
    x = np.arange(len(by_type))
    w = 0.4
    ax1.bar(x - w / 2, by_type["catches"], w, color="#a23b3b", label="Catches")
    ax1.set_ylabel("Catches", color="#a23b3b")
    ax1.set_xticks(x)
    ax1.set_xticklabels(by_type.index, rotation=20, ha="right")

    ax2 = ax1.twinx()
    ax2.bar(x + w / 2, by_type["rate_pct"], w, color="#3b6ea2", label="Catch rate (%)")
    ax2.set_ylabel("Catch rate (% of visits)", color="#3b6ea2")
    ax2.grid(False)

    ax1.set_title("Catches and catch-rate by trap type")
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
    fig.autofmt_xdate()
    return _fig_to_buf(fig)


def plot_top_traps(top: pd.Series) -> io.BytesIO:
    fig, ax = plt.subplots()
    if top.empty:
        ax.text(0.5, 0.5, "No catches recorded", ha="center", va="center")
        ax.set_axis_off()
    else:
        t = top.sort_values()
        ax.barh(t.index, t.values, color="#3b6ea2")
        for i, v in enumerate(t.values):
            ax.text(v, i, f" {v}", va="center")
        ax.set_xlabel("Catches")
        ax.set_title(f"Top {len(t)} traps by catches")
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


def plot_by_line(df: pd.DataFrame) -> io.BytesIO:
    labelled = df.copy()
    labelled["line"] = labelled["line"].where(
        labelled["line"].notna() & (labelled["line"] != ""),
        "(unassigned)",
    )
    by_line = labelled.groupby("line").agg(
        visits=("strikes", "size"),
        catches=("strikes", "sum"),
    ).sort_index()
    fig, ax1 = plt.subplots()
    x = np.arange(len(by_line))
    w = 0.4
    ax1.bar(x - w / 2, by_line["catches"], w, color="#a23b3b", label="Catches")
    ax1.set_ylabel("Catches", color="#a23b3b")
    ax1.set_xticks(x)
    ax1.set_xticklabels(by_line.index)
    ax2 = ax1.twinx()
    rate = by_line["catches"] / by_line["visits"] * 100
    ax2.bar(x + w / 2, rate, w, color="#3b6ea2", label="Catch rate (%)")
    ax2.set_ylabel("Catch rate (% of visits)", color="#3b6ea2")
    ax2.grid(False)
    ax1.set_title("Catches by trap line")
    return _fig_to_buf(fig)


def make_plots(df: pd.DataFrame, stats: dict) -> dict:
    plots = {
        "species": plot_species(stats["species"]),
        "by_type": plot_by_type(stats["by_type"]),
        "over_time": plot_over_time(df),
        "top_traps": plot_top_traps(stats["top_traps"]),
        "status": plot_status(stats["status"]),
    }
    if "line" in df.columns:
        line_rows = df[df["line"].notna() & (df["line"] != "")]
        if not line_rows.empty:
            plots["by_line"] = plot_by_line(df)
    return plots


# ---------- pdf ----------

def _img(buf: io.BytesIO) -> Image:
    return Image(buf, width=PLOT_W_IN * inch, height=PLOT_H_IN * inch)


def _kv_table(rows: list[tuple[str, str]]) -> Table:
    t = Table(rows, hAlign="LEFT", colWidths=[2.2 * inch, 3.0 * inch])
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, -1), "Helvetica", 10),
        ("FONT", (0, 0), (0, -1), "Helvetica-Bold", 10),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
    ]))
    return t


def _grid_table(header: list[str], rows: list[list[str]]) -> Table:
    data = [header] + rows
    t = Table(data, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, -1), "Helvetica", 9),
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
    ]))
    return t


def build_pdf(stats: dict, plots: dict, source_name: str, out_path: Path) -> None:
    styles = getSampleStyleSheet()
    h1 = styles["Heading1"]
    h2 = styles["Heading2"]
    body = styles["BodyText"]
    small = ParagraphStyle("small", parent=body, fontSize=8, textColor=colors.grey)

    story = []

    story.append(Paragraph(f"Trap report: {source_name}", h1))
    date_min = stats["date_min"].strftime("%Y-%m-%d")
    date_max = stats["date_max"].strftime("%Y-%m-%d")
    story.append(Paragraph(
        f"Visits between {date_min} and {date_max}. "
        f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}.",
        small,
    ))
    story.append(Spacer(1, 0.2 * inch))

    story.append(Paragraph("Summary", h2))
    story.append(_kv_table([
        ("Total visits", f"{stats['total_visits']}"),
        ("Unique traps", f"{stats['unique_traps']}"),
        ("Total catches", f"{stats['total_catches']}"),
        ("Overall catch rate", f"{stats['rate_pct']:.1f}% of visits"),
        ("Date range", f"{date_min} — {date_max}"),
    ]))
    story.append(Spacer(1, 0.25 * inch))

    story.append(Paragraph("Catches by species", h2))
    story.append(_img(plots["species"]))
    if not stats["species"].empty:
        rows = [[sp, str(int(n))] for sp, n in stats["species"].items()]
        story.append(Spacer(1, 0.1 * inch))
        story.append(_grid_table(["Species", "Catches"], rows))
    story.append(PageBreak())

    story.append(Paragraph("Trap-type performance", h2))
    story.append(_img(plots["by_type"]))
    bt = stats["by_type"]
    rows = [[idx, str(int(r.visits)), str(int(r.catches)), f"{r.rate_pct:.1f}%"]
            for idx, r in bt.iterrows()]
    story.append(Spacer(1, 0.1 * inch))
    story.append(_grid_table(["Trap type", "Visits", "Catches", "Rate"], rows))
    story.append(PageBreak())

    story.append(Paragraph("Catches over time", h2))
    story.append(_img(plots["over_time"]))
    story.append(PageBreak())

    story.append(Paragraph("Top traps", h2))
    story.append(_img(plots["top_traps"]))
    story.append(PageBreak())

    story.append(Paragraph("Trap status", h2))
    story.append(_img(plots["status"]))

    if "by_line" in plots:
        story.append(PageBreak())
        story.append(Paragraph("Per trap-line breakdown", h2))
        story.append(_img(plots["by_line"]))

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        title=f"Trap report — {source_name}",
    )
    doc.build(story)


# ---------- pipeline ----------

def process(csv_path: Path) -> Path:
    df = load_visits(csv_path)
    stats = compute_stats(df)
    plots = make_plots(df, stats)
    out_path = csv_path.with_name(f"{csv_path.stem}_report.pdf")
    build_pdf(stats, plots, csv_path.name, out_path)
    return out_path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("csv", nargs="?", type=Path, help="CSV file to report on")
    p.add_argument("-a", "--all", action="store_true",
                   help="Process every *.csv in the current directory")
    args = p.parse_args(argv)

    if args.all and args.csv is not None:
        p.error("give a CSV path OR -a, not both")
    if not args.all and args.csv is None:
        p.error("give a CSV path or use -a")

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
            out = process(path)
        except Exception as e:
            print(f"FAILED {path}: {e}", file=sys.stderr)
            return 2
        print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
Research Progress Visualizer
============================
Reads results.tsv and produces a multi-panel chart showing:
  - Score progression with color-coded status markers
  - Win rate and compounded P&L over iterations
  - Annotations on every KEEP (improvement) event

Usage:
    python3 plot_research.py              # saves research_progress.png + shows window
    python3 plot_research.py --no-show   # save only
"""

import argparse
import math
import textwrap
from pathlib import Path

import pandas as pd
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.lines import Line2D

BASE        = Path(__file__).parent.resolve()
RESULTS_TSV = BASE / "results.tsv"
OUT_PNG     = BASE / "research_progress.png"


# ---------------------------------------------------------------------------
# Load & clean
# ---------------------------------------------------------------------------

def load_results() -> pd.DataFrame:
    df = pd.read_csv(RESULTS_TSV, sep="\t")
    # Remove -inf rows for plotting (keep them flagged)
    df["score_raw"] = df["score"].astype(str)
    df["score_num"] = pd.to_numeric(df["score"], errors="coerce")
    df["is_valid"]  = df["score_num"].notna() & df["score_num"].ne(-math.inf)

    # Global sequential index across all runs
    df = df.reset_index(drop=True)
    df["seq"] = df.index
    return df


# ---------------------------------------------------------------------------
# Color / marker scheme
# ---------------------------------------------------------------------------

STATUS_STYLE = {
    "baseline":  dict(color="#4A90D9", marker="D", ms=9,  zorder=5, label="Baseline"),
    "keep":      dict(color="#27AE60", marker="*", ms=14, zorder=6, label="KEEP (improvement)"),
    "discard":   dict(color="#E67E22", marker="o", ms=6,  zorder=4, label="Discard"),
    "no_trades": dict(color="#8E44AD", marker="x", ms=8,  zorder=4, label="No trades"),
    "crash":     dict(color="#C0392B", marker="X", ms=8,  zorder=4, label="Crash"),
    "api_error": dict(color="#BDC3C7", marker=".",  ms=4,  zorder=3, label="API error"),
    "rate_limit":dict(color="#BDC3C7", marker=".",  ms=4,  zorder=3, label="Rate limit"),
}

def style(status: str) -> dict:
    return STATUS_STYLE.get(status, dict(color="grey", marker=".", ms=4, zorder=3, label=status))


# ---------------------------------------------------------------------------
# Plot
# ---------------------------------------------------------------------------

def get_index_yield(df: pd.DataFrame) -> float:
    """Derive index yield from the TSV: baseline compounded_pnl = index yield context.
    We parse it from the simulator output stored in results.tsv by looking at
    what the score formula implies, or fall back to 0 if not determinable."""
    # The simulator prints index_yield in KPIs; auto-researcher stores it in results.tsv
    # if a column exists, else derive from the score formula for baseline rows.
    if "index_yield" in df.columns:
        val = pd.to_numeric(df["index_yield"], errors="coerce").dropna()
        if not val.empty:
            return float(val.iloc[-1])
    return 0.0


def plot(df: pd.DataFrame, show: bool):
    fig = plt.figure(figsize=(18, 12), facecolor="#1A1A2E")
    fig.suptitle("TA35 Auto-Researcher — Optimization Progress",
                 fontsize=16, fontweight="bold", color="white", y=0.98)

    INDEX_YIELD = get_index_yield(df)

    gs = fig.add_gridspec(3, 1, hspace=0.45, left=0.07, right=0.97, top=0.93, bottom=0.07)
    ax_score = fig.add_subplot(gs[0])
    ax_pnl   = fig.add_subplot(gs[1])
    ax_wr    = fig.add_subplot(gs[2])

    for ax in (ax_score, ax_pnl, ax_wr):
        ax.set_facecolor("#16213E")
        ax.tick_params(colors="white")
        ax.xaxis.label.set_color("white")
        ax.yaxis.label.set_color("white")
        ax.title.set_color("white")
        for spine in ax.spines.values():
            spine.set_edgecolor("#334155")
        ax.grid(color="#2D3748", linestyle="--", linewidth=0.5, alpha=0.7)

    valid = df[df["is_valid"]]
    seq   = df["seq"]

    # ── Panel 1: Score ──────────────────────────────────────────────────────
    ax_score.set_title("Score Progression  (higher = better)", fontsize=11)
    ax_score.set_ylabel("Score", color="white")

    # Running best line
    best_so_far = []
    best = -math.inf
    for _, row in df.iterrows():
        if row["is_valid"] and row["score_num"] > best:
            best = row["score_num"]
        best_so_far.append(best if best != -math.inf else None)
    df["best_so_far"] = best_so_far

    ax_score.plot(df["seq"], df["best_so_far"],
                  color="#F39C12", lw=2, alpha=0.8, label="Running best", zorder=2)

    # Zero / index reference lines
    ax_score.axhline(0, color="#E74C3C", lw=1, linestyle=":", alpha=0.6, label="Score = 0")

    # Points by status
    plotted_labels = set()
    for status, grp in df.groupby("status"):
        st = style(status)
        grp_valid = grp[grp["is_valid"]]
        if grp_valid.empty:
            continue
        label = st["label"] if st["label"] not in plotted_labels else "_nolegend_"
        plotted_labels.add(st["label"])
        ax_score.scatter(grp_valid["seq"], grp_valid["score_num"],
                         color=st["color"], marker=st["marker"], s=st["ms"]**2,
                         zorder=st["zorder"], label=label, alpha=0.9)

    # Annotate KEEP events
    keeps = df[(df["status"] == "keep") & df["is_valid"]]
    for _, row in keeps.iterrows():
        desc = str(row.get("description", ""))
        short = textwrap.shorten(desc.strip('"'), width=45, placeholder="…")
        ax_score.annotate(
            f'+{row["score_num"]:.0f}\n{short}',
            xy=(row["seq"], row["score_num"]),
            xytext=(12, 8), textcoords="offset points",
            fontsize=6.5, color="#27AE60",
            arrowprops=dict(arrowstyle="->", color="#27AE60", lw=0.8),
            bbox=dict(boxstyle="round,pad=0.3", fc="#0F3460", ec="#27AE60", alpha=0.85),
        )

    ax_score.legend(loc="upper left", fontsize=8, facecolor="#1A1A2E",
                    labelcolor="white", framealpha=0.7)

    # ── Panel 2: Compounded P&L vs Index ────────────────────────────────────
    ax_pnl.set_title("Compounded P&L vs TA35 Buy-and-Hold", fontsize=11)
    ax_pnl.set_ylabel("Compounded P&L %", color="white")

    ax_pnl.axhline(INDEX_YIELD, color="#E74C3C", lw=1.5, linestyle="--",
                   label=f"Index yield {INDEX_YIELD:+.1f}%")

    # All valid dots coloured by status
    for status, grp in df.groupby("status"):
        st = style(status)
        grp_v = grp[grp["is_valid"]]
        if grp_v.empty:
            continue
        ax_pnl.scatter(grp_v["seq"], grp_v["compounded_pnl"],
                       color=st["color"], marker=st["marker"], s=st["ms"]**2,
                       zorder=st["zorder"], alpha=0.85)

    # Running best P&L line
    best_pnl = []
    b = None
    for _, row in df.iterrows():
        if row["status"] in ("keep", "baseline") and row["is_valid"]:
            b = row["compounded_pnl"]
        best_pnl.append(b)
    ax_pnl.plot(df["seq"], best_pnl, color="#F39C12", lw=2, alpha=0.8, label="Best P&L so far")

    # Shade above/below index
    best_arr = pd.Series(best_pnl).ffill().values
    ax_pnl.fill_between(df["seq"], best_arr, INDEX_YIELD,
                        where=(best_arr > INDEX_YIELD),
                        color="#27AE60", alpha=0.15, label="Beating index")
    ax_pnl.fill_between(df["seq"], best_arr, INDEX_YIELD,
                        where=(best_arr <= INDEX_YIELD),
                        color="#E74C3C", alpha=0.10, label="Lagging index")

    ax_pnl.legend(loc="upper left", fontsize=8, facecolor="#1A1A2E",
                  labelcolor="white", framealpha=0.7)

    # ── Panel 3: Win Rate ────────────────────────────────────────────────────
    ax_wr.set_title("Win Rate per Iteration", fontsize=11)
    ax_wr.set_ylabel("Win Rate %", color="white")
    ax_wr.set_xlabel("Iteration (global seq)", color="white")

    ax_wr.axhline(50, color="#F39C12", lw=1, linestyle=":", alpha=0.7, label="50% WR target")
    ax_wr.axhline(80, color="#27AE60", lw=1, linestyle=":", alpha=0.5, label="80% WR")

    for status, grp in df.groupby("status"):
        st = style(status)
        grp_v = grp[grp["is_valid"]]
        if grp_v.empty:
            continue
        ax_wr.scatter(grp_v["seq"], grp_v["win_rate"],
                      color=st["color"], marker=st["marker"], s=st["ms"]**2,
                      zorder=st["zorder"], alpha=0.85)

    # Best WR line (for keeps only)
    best_wr = []
    b = None
    for _, row in df.iterrows():
        if row["status"] in ("keep", "baseline") and row["is_valid"]:
            b = row["win_rate"]
        best_wr.append(b)
    ax_wr.plot(df["seq"], best_wr, color="#F39C12", lw=2, alpha=0.8, label="Best WR so far")

    ax_wr.legend(loc="upper left", fontsize=8, facecolor="#1A1A2E",
                 labelcolor="white", framealpha=0.7)

    # ── Stats box ────────────────────────────────────────────────────────────
    keeps_df = df[(df["status"] == "keep") & df["is_valid"]]
    if not keeps_df.empty:
        best_row = keeps_df.loc[keeps_df["score_num"].idxmax()]
        stats = (
            f"Best score : {best_row['score_num']:.2f}\n"
            f"Win rate   : {best_row['win_rate']:.1f}%\n"
            f"Comp. P&L  : {best_row['compounded_pnl']:+.2f}%\n"
            f"vs Index   : {best_row['compounded_pnl'] - INDEX_YIELD:+.2f}%\n"
            f"Trades     : {int(best_row['total_trades'])}\n"
            f"Iterations : {len(df)}"
        )
        fig.text(0.985, 0.96, stats, transform=fig.transFigure,
                 fontsize=8.5, color="white", va="top", ha="right",
                 bbox=dict(boxstyle="round", fc="#0F3460", ec="#27AE60", alpha=0.9),
                 family="monospace")

    plt.savefig(OUT_PNG, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    print(f"[PLOT] Saved to {OUT_PNG}")

    if show:
        plt.show()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-show", action="store_true", help="Save only, don't open window")
    args = parser.parse_args()

    if not RESULTS_TSV.exists():
        print(f"[ERROR] {RESULTS_TSV} not found. Run auto-researcher.py first.")
        raise SystemExit(1)

    matplotlib.rcParams["axes.unicode_minus"] = False
    df = load_results()
    plot(df, show=not args.no_show)

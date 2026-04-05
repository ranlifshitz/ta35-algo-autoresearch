"""
Auto Researcher — TA35 Algo Optimizer
======================================
Inspired by Karpathy's autoresearch pattern.

Autonomously optimizes Algo-machine.py by:
  1. Running algo-simulator.py to get KPI results
  2. Feeding results + current code to Claude (market data never exposed)
  3. Applying Claude's suggested code modifications
  4. Scoring: primary = win rate ≥ 75%, secondary = max compounded P&L
  5. Keeping improvements, reverting failures
  6. Logging every iteration to results.tsv

Usage:
  export ANTHROPIC_API_KEY=sk-ant-...
  python3 auto-researcher.py [--iterations N] [--model MODEL]

Targets:
  Win rate ≥ 75%   (primary)
  Max compounded return  (secondary)
"""

import os
import sys
import subprocess
import shutil
import re
import ast
import csv
import argparse
from datetime import datetime
from pathlib import Path

import anthropic

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE       = Path(__file__).parent.resolve()
ALGO_FILE  = BASE / "Algo-machine.py"
SIM_FILE   = BASE / "algo-simulator.py"
PROG_FILE  = BASE / "program.md"
RESULTS    = BASE / "results.tsv"
BACKUP_DIR = BASE / "research-history"

# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
WIN_RATE_TARGET = 75.0

def compute_score(win_rate: float, compounded_pnl: float, total_trades: int) -> float:
    """
    Primary goal: win_rate >= 75%
    Secondary:    maximize compounded_pnl

    Below 75% win rate: quadratic penalty dominates.
    At/above 75%:       pure compounded P&L + small bonus per extra % win rate.
    """
    if total_trades < 5:
        return float('-inf')
    if win_rate < WIN_RATE_TARGET:
        gap = WIN_RATE_TARGET - win_rate
        return compounded_pnl * 0.1 - gap ** 2      # heavy quadratic penalty
    else:
        bonus = (win_rate - WIN_RATE_TARGET) * 5    # extra win rate is rewarded
        return compounded_pnl + bonus


# ---------------------------------------------------------------------------
# KPI parsing — reads the simulator's stdout
# ---------------------------------------------------------------------------

def parse_kpis(output: str) -> dict:
    """Extract KPIs from simulator stdout. Returns dict or raises ValueError."""
    patterns = {
        "total_trades": r"Total trades\s*:\s*(\d+)",
        "wins":         r"Wins \(TP\)\s*:\s*(\d+)",
        "losses":       r"Losses \(SL\)\s*:\s*(\d+)",
        "win_rate":     r"Win rate\s*:\s*([\d.]+)%",
        "avg_pnl":      r"Avg P&L/trade\s*:\s*([+-]?[\d.]+)%",
        "total_pnl":    r"Total P&L\s*:\s*([+-]?[\d.]+)%",
        "compounded":   r"Compounded\s*:\s*([+-]?[\d.]+)%",
        "best_trade":   r"Best trade\s*:\s*([+-]?[\d.]+)%",
        "worst_trade":  r"Worst trade\s*:\s*([+-]?[\d.]+)%",
        "avg_hold":     r"Avg hold\s*:\s*([\d.]+) hours",
    }
    kpis = {}
    for key, pat in patterns.items():
        m = re.search(pat, output)
        if m:
            kpis[key] = float(m.group(1))
        else:
            kpis[key] = 0.0

    if kpis["total_trades"] == 0:
        raise ValueError("Simulator produced no trades — likely a code error.")
    return kpis


def format_kpis(kpis: dict) -> str:
    return (
        f"Total trades : {int(kpis['total_trades'])}\n"
        f"  Wins (TP)  : {int(kpis['wins'])}\n"
        f"  Losses (SL): {int(kpis['losses'])}\n"
        f"Win rate     : {kpis['win_rate']:.1f}%  (target: ≥75%)\n"
        f"Avg P&L/trade: {kpis['avg_pnl']:+.2f}%\n"
        f"Total P&L    : {kpis['total_pnl']:+.2f}%\n"
        f"Compounded   : {kpis['compounded']:+.2f}%\n"
        f"Best trade   : {kpis['best_trade']:+.2f}%\n"
        f"Worst trade  : {kpis['worst_trade']:+.2f}%\n"
        f"Avg hold     : {kpis['avg_hold']:.1f} hours"
    )


# ---------------------------------------------------------------------------
# Running the simulator
# ---------------------------------------------------------------------------

def run_simulator(timeout: int = 120) -> tuple[str, str, int]:
    """Run algo-simulator.py. Returns (stdout, stderr, returncode)."""
    result = subprocess.run(
        [sys.executable, str(SIM_FILE)],
        capture_output=True, text=True,
        cwd=str(BASE), timeout=timeout
    )
    return result.stdout, result.stderr, result.returncode


# ---------------------------------------------------------------------------
# Code extraction from Claude's response
# ---------------------------------------------------------------------------

def extract_code(response_text: str) -> str | None:
    """Extract code between <algo_machine>...</algo_machine> tags."""
    m = re.search(r"<algo_machine>(.*?)</algo_machine>", response_text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Fallback: try triple-backtick python block
    m = re.search(r"```python\s*(class TA35AlgoMachine.*?)```", response_text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return None


def validate_code(code: str) -> tuple[bool, str]:
    """Syntax-check the code and verify required interface elements."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"SyntaxError: {e}"

    # Check class exists
    classes = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    if "TA35AlgoMachine" not in classes:
        return False, "Missing class TA35AlgoMachine"

    # Check process_hour method
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "TA35AlgoMachine":
            methods = [n.name for n in ast.walk(node) if isinstance(n, ast.FunctionDef)]
            if "process_hour" not in methods:
                return False, "Missing method process_hour"
            if "reset" not in methods:
                return False, "Missing method reset"

    return True, "OK"


# ---------------------------------------------------------------------------
# Results log
# ---------------------------------------------------------------------------

def init_results_log():
    if not RESULTS.exists():
        with open(RESULTS, "w", newline="") as f:
            w = csv.writer(f, delimiter="\t")
            w.writerow(["iteration", "timestamp", "win_rate", "compounded_pnl",
                        "total_trades", "score", "status", "description"])


def log_result(iteration: int, kpis: dict, score: float, status: str, description: str):
    with open(RESULTS, "a", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow([
            iteration,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            f"{kpis['win_rate']:.1f}",
            f"{kpis['compounded']:.2f}",
            int(kpis['total_trades']),
            f"{score:.2f}",
            status,
            description,
        ])


# ---------------------------------------------------------------------------
# History summary for the researcher prompt
# ---------------------------------------------------------------------------

def build_history_summary(max_rows: int = 10) -> str:
    if not RESULTS.exists():
        return "No history yet."
    rows = []
    with open(RESULTS, newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            rows.append(row)
    if not rows:
        return "No history yet."
    recent = rows[-max_rows:]
    lines = ["iter | win_rate | compounded | trades | score | status"]
    lines.append("-" * 58)
    for r in recent:
        lines.append(f"  {r['iteration']:>3} | {r['win_rate']:>7}% | {r['compounded']:>9}% | "
                     f"{r['total_trades']:>6} | {r['score']:>7} | {r['status']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def backup_algo(iteration: int, label: str):
    BACKUP_DIR.mkdir(exist_ok=True)
    dst = BACKUP_DIR / f"Algo-machine_iter{iteration:04d}_{label}.py"
    shutil.copy2(ALGO_FILE, dst)


# ---------------------------------------------------------------------------
# Main research loop
# ---------------------------------------------------------------------------

def research_loop(n_iterations: int, model: str, api_key: str):
    client = anthropic.Anthropic(api_key=api_key)

    # Load researcher instructions
    program_md = PROG_FILE.read_text()

    # Initialise log
    init_results_log()
    BACKUP_DIR.mkdir(exist_ok=True)

    best_score   = float('-inf')
    best_code    = ALGO_FILE.read_text()
    best_kpis    = {}

    print("=" * 70)
    print("  TA35 AUTO RESEARCHER")
    print(f"  Model: {model}   |   Max iterations: {n_iterations}")
    print(f"  Target: ≥{WIN_RATE_TARGET:.0f}% win rate + max compounded P&L")
    print("=" * 70)

    # Run baseline
    print("\n[BASELINE] Running simulator...")
    stdout, stderr, rc = run_simulator()
    if rc != 0 or not stdout:
        print(f"[ERROR] Baseline simulator failed:\n{stderr}")
        sys.exit(1)
    try:
        best_kpis  = parse_kpis(stdout)
        best_score = compute_score(best_kpis['win_rate'], best_kpis['compounded'], int(best_kpis['total_trades']))
        print(f"[BASELINE] Score={best_score:.2f}\n{format_kpis(best_kpis)}")
        log_result(0, best_kpis, best_score, "baseline", "Initial state")
        backup_algo(0, "baseline")
    except ValueError as e:
        print(f"[ERROR] {e}\n{stderr}")
        sys.exit(1)

    # -----------------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------------
    for iteration in range(1, n_iterations + 1):
        print(f"\n{'─' * 70}")
        print(f"  ITERATION {iteration}/{n_iterations}")
        print(f"  Best so far: score={best_score:.2f}, "
              f"WR={best_kpis['win_rate']:.1f}%, cPnL={best_kpis['compounded']:.2f}%")
        print(f"{'─' * 70}")

        current_code = ALGO_FILE.read_text()

        # --- Build prompt ---
        user_prompt = f"""## Current Algo-machine.py

```python
{current_code}
```

## Current performance KPIs
{format_kpis(best_kpis)}
Score: {best_score:.2f}  (negative = below 75% win rate target)

## Experiment history
{build_history_summary()}

---
Analyze the results. Identify the most likely cause of under-performance.
Propose ONE focused change that most likely moves win rate toward ≥75% while
maintaining or growing compounded P&L.

Output your analysis (2-4 sentences) then the complete new Algo-machine.py
inside <algo_machine>...</algo_machine> tags.
"""

        # --- Call Claude ---
        print("[RESEARCHER] Calling Claude API...")
        try:
            response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=program_md,
                messages=[{"role": "user", "content": user_prompt}]
            )
            response_text = response.content[0].text
        except Exception as e:
            print(f"[ERROR] API call failed: {e}")
            log_result(iteration, best_kpis, float('-inf'), "api_error", str(e)[:80])
            continue

        # Print researcher's analysis (first paragraph before the code block)
        analysis = response_text.split("<algo_machine>")[0].strip()
        print(f"\n[RESEARCHER ANALYSIS]\n{analysis}\n")

        # --- Extract and validate code ---
        new_code = extract_code(response_text)
        if not new_code:
            print("[WARN] Could not extract code from response. Skipping.")
            log_result(iteration, best_kpis, float('-inf'), "parse_error", "No code extracted")
            continue

        valid, reason = validate_code(new_code)
        if not valid:
            print(f"[WARN] Code validation failed: {reason}. Skipping.")
            log_result(iteration, best_kpis, float('-inf'), "invalid_code", reason[:80])
            continue

        # --- Apply new code ---
        backup_algo(iteration, "candidate")
        ALGO_FILE.write_text(new_code)

        # --- Evaluate ---
        print("[SIM] Running simulator with new code...")
        stdout, stderr, rc = run_simulator()

        if rc != 0 or not stdout:
            print(f"[WARN] Simulator crashed. Reverting.\n{stderr[:300]}")
            ALGO_FILE.write_text(best_code)
            log_result(iteration, best_kpis, float('-inf'), "crash", stderr[:80])
            continue

        try:
            new_kpis  = parse_kpis(stdout)
            new_score = compute_score(new_kpis['win_rate'], new_kpis['compounded'], int(new_kpis['total_trades']))
        except ValueError as e:
            print(f"[WARN] {e}. Reverting.")
            ALGO_FILE.write_text(best_code)
            log_result(iteration, best_kpis, float('-inf'), "no_trades", str(e)[:80])
            continue

        print(f"[RESULT] New score={new_score:.2f}  |  {format_kpis(new_kpis)}")

        # --- Keep or revert ---
        if new_score > best_score:
            delta = new_score - best_score
            print(f"[KEEP] Score improved by {delta:.2f}. Keeping new code.")
            best_score = new_score
            best_code  = new_code
            best_kpis  = new_kpis
            backup_algo(iteration, "best")
            description = analysis[:80].replace("\n", " ")
            log_result(iteration, new_kpis, new_score, "keep", description)

            # Check if target reached
            if new_kpis['win_rate'] >= WIN_RATE_TARGET:
                print(f"\n{'=' * 70}")
                print(f"  TARGET REACHED! Win rate = {new_kpis['win_rate']:.1f}% ≥ 75%")
                print(f"  Compounded P&L = {new_kpis['compounded']:.2f}%")
                print(f"  Continuing to maximize P&L...")
                print(f"{'=' * 70}")
        else:
            print(f"[DISCARD] Score regressed ({new_score:.2f} < {best_score:.2f}). Reverting.")
            ALGO_FILE.write_text(best_code)
            description = analysis[:80].replace("\n", " ")
            log_result(iteration, new_kpis, new_score, "discard", description)

    # --- Final report ---
    print(f"\n{'=' * 70}")
    print("  RESEARCH COMPLETE")
    print(f"{'=' * 70}")
    print(f"  Final best score    : {best_score:.2f}")
    print(f"  Win rate            : {best_kpis['win_rate']:.1f}%  (target: ≥75%)")
    print(f"  Compounded P&L      : {best_kpis['compounded']:.2f}%")
    print(f"  Total trades        : {int(best_kpis['total_trades'])}")
    print(f"  Best code saved to  : {ALGO_FILE}")
    print(f"  Full history        : {RESULTS}")
    print(f"  Iteration backups   : {BACKUP_DIR}/")
    if best_kpis['win_rate'] < WIN_RATE_TARGET:
        print(f"\n  [!] Win rate target NOT yet reached. Run more iterations.")
    else:
        print(f"\n  [✓] Win rate target achieved.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TA35 Algo Auto Researcher")
    parser.add_argument("--iterations", type=int, default=20,
                        help="Number of research iterations (default: 20)")
    parser.add_argument("--model", type=str, default="claude-sonnet-4-6",
                        help="Claude model to use (default: claude-sonnet-4-6)")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("[ERROR] ANTHROPIC_API_KEY environment variable not set.")
        print("  export ANTHROPIC_API_KEY=sk-ant-...")
        sys.exit(1)

    research_loop(args.iterations, args.model, api_key)

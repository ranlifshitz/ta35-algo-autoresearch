"""
Auto Researcher — TA35 Algo Optimizer
======================================
Inspired by Karpathy's autoresearch pattern.

Autonomously optimizes Algo-machine.py by:
  1. Running algo-simulator.py to get KPI results
  2. Feeding results + current code to Gemma 4 (market data never exposed)
  3. Applying Gemma's suggested code modifications
  4. Scoring: primary = win rate ≥ 75%, secondary = max compounded P&L
  5. Keeping improvements, reverting failures
  6. Logging every iteration to results.tsv

Usage:
  export GOOGLE_API_KEY=AIza...   # Google AI Studio free key
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
import time
import argparse
from datetime import datetime
from pathlib import Path

from google import genai
from google.genai import types

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
WIN_RATE_TARGET = 50.0

def compute_score(win_rate: float, compounded_pnl: float, total_trades: int,
                  period_months: float = 1.0, index_yield: float = 0.0) -> float:
    """
    Base  : alpha over buy-and-hold (compounded_pnl - index_yield)
    Bonus : +5 points per % of win rate above 50% (no penalty below 50%)
    Floor : fewer than 1 trade/month → invalid (-inf)

    Win rate is purely a bonus — beating the index is the primary objective.
    """
    min_trades = max(1, int(period_months))
    if total_trades < min_trades:
        return float('-inf')
    excess   = compounded_pnl - index_yield          # alpha: positive = beating index
    wr_bonus = max(0.0, win_rate - WIN_RATE_TARGET) * 5  # bonus only when WR > 50%
    return excess + wr_bonus


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
        "index_yield":  r"Index yield\s*:\s*([+-]?[\d.]+)%",
        "vs_index":     r"vs Index\s*:\s*([+-]?[\d.]+)%",
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

    # Parse simulation period to derive minimum required trades (≥1/month)
    period_m = re.search(r"Period\s*:\s*(\d{4}-\d{2}-\d{2})\s*->\s*(\d{4}-\d{2}-\d{2})", output)
    if period_m:
        from datetime import date
        d0 = date.fromisoformat(period_m.group(1))
        d1 = date.fromisoformat(period_m.group(2))
        months = (d1.year - d0.year) * 12 + (d1.month - d0.month) + 1
        kpis["period_months"] = float(months)
    else:
        kpis["period_months"] = 1.0

    if kpis["total_trades"] == 0:
        raise ValueError("Simulator produced no trades — likely a code error.")
    return kpis


def format_kpis(kpis: dict) -> str:
    months = int(kpis.get('period_months', 1))
    min_t  = max(1, months)
    beating = "BEATING" if kpis.get('vs_index', -999) > 0 else "LAGGING"
    return (
        f"Period       : {months} months  (min trades required: {min_t})\n"
        f"Index yield  : {kpis.get('index_yield', 0):+.2f}%  ← target to beat\n"
        f"Compounded   : {kpis['compounded']:+.2f}%\n"
        f"vs Index     : {kpis.get('vs_index', 0):+.2f}%  [{beating} the index]\n"
        f"Total trades : {int(kpis['total_trades'])}\n"
        f"  Wins (TP)  : {int(kpis['wins'])}\n"
        f"  Losses (SL): {int(kpis['losses'])}\n"
        f"Win rate     : {kpis['win_rate']:.1f}%  (target: ≥50%)\n"
        f"Avg P&L/trade: {kpis['avg_pnl']:+.2f}%\n"
        f"Total P&L    : {kpis['total_pnl']:+.2f}%\n"
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
        lines.append(f"  {r['iteration']:>3} | {r['win_rate']:>7}% | {r['compounded_pnl']:>9}% | "
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
# Context compaction
# ---------------------------------------------------------------------------

CONTEXT_LIMIT   = 160_000   # estimated tokens — compact well before the 262k hard limit
CHARS_PER_TOKEN = 3         # conservative: real ratio is ~3 chars/token for code-heavy prompts

def estimate_tokens(conversation: list[dict]) -> int:
    total = sum(len(turn["parts"][0]["text"]) for turn in conversation)
    return total // CHARS_PER_TOKEN


def compact_conversation(client, model: str, program_md: str,
                         conversation: list[dict],
                         best_code: str, best_kpis: dict, best_score: float,
                         last_kpis: dict, last_score: float, last_status: str) -> list[dict]:
    """
    Summarise the conversation so far into a compact research journal,
    then restart the conversation with that summary as the opening context.
    Uses a separate Gemma call so the main conversation thread is not polluted.
    """
    print("[COMPACT] Context approaching limit — compacting conversation history...")

    # Build a plain-text dump of the conversation for summarisation
    history_text = ""
    for turn in conversation:
        role = turn["role"].upper()
        text = turn["parts"][0]["text"]
        # Skip the very long opening turn (instructions + baseline code) — we'll re-add fresh
        if len(text) > 8000 and role == "USER" and "Baseline Algo-machine.py" in text:
            history_text += "[USER: initial instructions + baseline code — omitted for brevity]\n\n"
        else:
            history_text += f"[{role}]:\n{text[:3000]}{'...(truncated)' if len(text)>3000 else ''}\n\n"

    summary_prompt = (
        "You are summarising a quantitative trading research session for context compaction.\n"
        "Below is the conversation history of an LLM that has been iteratively improving "
        "a TA35 index trading algorithm.\n\n"
        "Write a concise RESEARCH JOURNAL (max 1500 words) that captures:\n"
        "1. What approaches were tried (summarise each experiment in 1-2 lines)\n"
        "2. Which ones were KEPT vs DISCARDED and why (based on the KPI feedback)\n"
        "3. Key insights discovered about what works and what doesn't\n"
        "4. The current best strategy's logic and parameters\n"
        "5. Promising directions NOT yet tried\n\n"
        "Be specific about numbers (win rate, score, vs-index). "
        "This summary will replace the full history — make it actionable.\n\n"
        f"=== CONVERSATION HISTORY ===\n{history_text}"
    )

    summary = "[Summary unavailable — compaction call failed]"
    try:
        resp = client.models.generate_content(
            model=model,
            contents=summary_prompt,
            config=types.GenerateContentConfig(max_output_tokens=2048, temperature=0.3),
        )
        summary = resp.text
        print(f"[COMPACT] Summary generated ({len(summary)} chars).")
    except Exception as e:
        print(f"[COMPACT] Warning: summary call failed ({e}). Using minimal stub.")

    # Rebuild conversation: fresh opening with summary + current state
    new_opening = (
        f"{program_md}\n\n---\n\n"
        f"## Research Journal (compacted from prior iterations)\n\n{summary}\n\n"
        f"## Current best Algo-machine.py\n\n```python\n{best_code}\n```\n\n"
        f"## Current best KPIs\n{format_kpis(best_kpis)}\nBest score: {best_score:.2f}\n\n"
        f"## Last experiment result\n"
        f"Status: {last_status} | Score: {last_score:.2f}\n"
        f"{format_kpis(last_kpis)}\n\n"
        f"The conversation history has been compacted. Continue from here."
    )
    new_conversation = [{"role": "user", "parts": [{"text": new_opening}]}]
    tokens_after = estimate_tokens(new_conversation)
    print(f"[COMPACT] Done. Tokens before: ~{estimate_tokens(conversation):,} → after: ~{tokens_after:,}")
    return new_conversation


# ---------------------------------------------------------------------------
# Main research loop
# ---------------------------------------------------------------------------

def research_loop(n_iterations: int, model: str, api_key: str):
    client = genai.Client(api_key=api_key)

    # Load researcher instructions
    program_md = PROG_FILE.read_text()

    # Initialise log
    init_results_log()
    BACKUP_DIR.mkdir(exist_ok=True)

    best_score   = float('-inf')
    best_code    = ALGO_FILE.read_text()
    best_kpis    = {}

    # Tracks the actual result of the last attempt (may differ from best if discarded)
    last_kpis    = {}
    last_score   = float('-inf')
    last_status  = "none"

    # Persistent conversation history — model remembers all its reasoning across iterations
    conversation: list[dict] = []

    print("=" * 70)
    print("  TA35 AUTO RESEARCHER")
    print(f"  Model: {model}   |   Max iterations: {n_iterations}")
    print(f"  Target: beat index yield, WR >50% is a bonus")
    print("=" * 70)

    # Run baseline
    print("\n[BASELINE] Running simulator...")
    stdout, stderr, rc = run_simulator()
    if rc != 0 or not stdout:
        print(f"[ERROR] Baseline simulator failed:\n{stderr}")
        sys.exit(1)
    try:
        best_kpis  = parse_kpis(stdout)
        best_score = compute_score(best_kpis['win_rate'], best_kpis['compounded'],
                                   int(best_kpis['total_trades']), best_kpis['period_months'],
                                   best_kpis.get('index_yield', 0.0))
        last_kpis  = best_kpis
        last_score = best_score
        last_status = "baseline"
        print(f"[BASELINE] Score={best_score:.2f}\n{format_kpis(best_kpis)}")
        log_result(0, best_kpis, best_score, "baseline", "Initial state")
        backup_algo(0, "baseline")
    except ValueError as e:
        print(f"[ERROR] {e}\n{stderr}")
        sys.exit(1)

    # Seed the conversation with instructions + baseline state (first user turn)
    opening_turn = (
        f"{program_md}\n\n---\n\n"
        f"## Baseline Algo-machine.py\n\n```python\n{best_code}\n```\n\n"
        f"## Baseline KPIs\n{format_kpis(best_kpis)}\n"
        f"Score: {best_score:.2f}\n\n"
        f"This is the starting point. Begin your first experiment."
    )
    conversation.append({"role": "user", "parts": [{"text": opening_turn}]})

    # -----------------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------------
    for iteration in range(1, n_iterations + 1):
        print(f"\n{'─' * 70}")
        print(f"  ITERATION {iteration}/{n_iterations}")
        print(f"  Best so far: score={best_score:.2f}, "
              f"WR={best_kpis['win_rate']:.1f}%, cPnL={best_kpis['compounded']:.2f}%, "
              f"vs Index={best_kpis.get('vs_index', 0):+.2f}%")
        print(f"{'─' * 70}")

        current_code = ALGO_FILE.read_text()

        # Each follow-up turn gives the model the ACTUAL result of its last attempt
        if iteration > 1:
            if last_status == "keep":
                verdict = f"✓ KEPT — new best (score {last_score:.2f})"
            elif last_status in ("discard", "crash", "no_trades"):
                verdict = (f"✗ DISCARDED — score {last_score:.2f} < best {best_score:.2f}. "
                           f"Code reverted to best.")
            else:
                verdict = f"⚠ SKIPPED ({last_status})"

            follow_up = (
                f"## Result of your last experiment: {verdict}\n\n"
                f"### KPIs achieved by your change:\n{format_kpis(last_kpis)}\n"
                f"Score: {last_score:.2f}\n\n"
                f"### Current best KPIs (what you need to beat):\n{format_kpis(best_kpis)}\n"
                f"Best score: {best_score:.2f}\n\n"
                f"### Current best Algo-machine.py\n\n```python\n{current_code}\n```\n\n"
                f"Based on what you learned, propose a DIFFERENT experiment. "
                f"Do not repeat the same change if it was discarded."
            )
            conversation.append({"role": "user", "parts": [{"text": follow_up}]})

        # --- Compact context if approaching token limit ---
        if estimate_tokens(conversation) > CONTEXT_LIMIT:
            conversation = compact_conversation(
                client, model, program_md, conversation,
                best_code, best_kpis, best_score,
                last_kpis, last_score, last_status,
            )

        # --- Call Gemma with full conversation history ---
        # Wrap in an outer retry so token-limit errors trigger compaction then retry
        response_text = None
        for context_attempt in range(3):
            print(f"[RESEARCHER] Calling Gemma (~{estimate_tokens(conversation):,} tokens)...")
            retry_delays = [60, 120, 240]   # seconds to wait on RESOURCE_EXHAUSTED
            for attempt, delay in enumerate([0] + retry_delays):
                if delay:
                    print(f"[RATE LIMIT] Resource exhausted — sleeping {delay}s before retry "
                          f"(attempt {attempt}/{len(retry_delays)})...")
                    time.sleep(delay)
                try:
                    response = client.models.generate_content(
                        model=model,
                        contents=conversation,
                        config=types.GenerateContentConfig(
                            max_output_tokens=4096,
                            temperature=0.7,
                        ),
                    )
                    response_text = response.text
                    break   # success — exit inner retry
                except Exception as e:
                    err_str = str(e)
                    if "token count exceeds" in err_str or (
                            "INVALID_ARGUMENT" in err_str and "token" in err_str.lower()):
                        # Context too large — compact and retry the outer loop
                        print(f"[COMPACT] Token limit hit mid-loop — forcing compaction...")
                        conversation = compact_conversation(
                            client, model, program_md, conversation,
                            best_code, best_kpis, best_score,
                            last_kpis, last_score, last_status,
                        )
                        break   # break inner loop, outer loop will retry
                    elif "RESOURCE_EXHAUSTED" in err_str or "429" in err_str:
                        if attempt < len(retry_delays):
                            continue    # will sleep and retry
                        print(f"[ERROR] Rate limit persists after all retries. Skipping iteration.")
                        log_result(iteration, best_kpis, float('-inf'), "rate_limit", err_str[:80])
                        break
                    elif "500" in err_str or "INTERNAL" in err_str:
                        print(f"[ERROR] Internal server error — sleeping 5s before retry...")
                        time.sleep(5)
                        continue    # retry immediately after brief sleep
                    else:
                        print(f"[ERROR] API call failed: {e}")
                        log_result(iteration, best_kpis, float('-inf'), "api_error", err_str[:80])
                        break   # non-retryable error

            if response_text is not None:
                break   # got a response — exit outer context_attempt loop

        if response_text is None:
            # Remove the user turn we just added so history stays consistent
            if iteration > 1:
                conversation.pop()
            continue

        # Append model response to conversation history
        conversation.append({"role": "model", "parts": [{"text": response_text}]})

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
            last_kpis   = best_kpis
            last_score  = float('-inf')
            last_status = "crash"
            log_result(iteration, best_kpis, float('-inf'), "crash", stderr[:80])
            continue

        try:
            new_kpis  = parse_kpis(stdout)
            new_score = compute_score(new_kpis['win_rate'], new_kpis['compounded'],
                                      int(new_kpis['total_trades']), new_kpis['period_months'],
                                      new_kpis.get('index_yield', 0.0))
        except ValueError as e:
            print(f"[WARN] {e}. Reverting.")
            ALGO_FILE.write_text(best_code)
            last_kpis   = best_kpis
            last_score  = float('-inf')
            last_status = "no_trades"
            log_result(iteration, best_kpis, float('-inf'), "no_trades", str(e)[:80])
            continue

        print(f"[RESULT] New score={new_score:.2f}  |  {format_kpis(new_kpis)}")

        # Record actual result so next follow-up turn is accurate
        last_kpis  = new_kpis
        last_score = new_score

        # --- Keep or revert ---
        if new_score > best_score:
            delta = new_score - best_score
            print(f"[KEEP] Score improved by {delta:.2f}. Keeping new code.")
            best_score  = new_score
            best_code   = new_code
            best_kpis   = new_kpis
            last_status = "keep"
            backup_algo(iteration, "best")
            description = analysis[:80].replace("\n", " ")
            log_result(iteration, new_kpis, new_score, "keep", description)

            if new_kpis['win_rate'] >= WIN_RATE_TARGET:
                print(f"\n{'=' * 70}")
                print(f"  WIN RATE TARGET REACHED! {new_kpis['win_rate']:.1f}% ≥ {WIN_RATE_TARGET:.0f}%")
                print(f"  Compounded P&L = {new_kpis['compounded']:.2f}%  vs Index = {new_kpis.get('vs_index',0):+.2f}%")
                print(f"  Continuing to maximize alpha...")
                print(f"{'=' * 70}")
        else:
            print(f"[DISCARD] Score regressed ({new_score:.2f} < {best_score:.2f}). Reverting.")
            ALGO_FILE.write_text(best_code)
            last_status = "discard"
            description = analysis[:80].replace("\n", " ")
            log_result(iteration, new_kpis, new_score, "discard", description)

        if iteration < n_iterations:
            print(f"[WAIT] Sleeping 3s before next iteration...")
            time.sleep(3)

    # --- Final report ---
    print(f"\n{'=' * 70}")
    print("  RESEARCH COMPLETE")
    print(f"{'=' * 70}")
    print(f"  Final best score    : {best_score:.2f}")
    print(f"  Win rate            : {best_kpis['win_rate']:.1f}%  (target: ≥50%)")
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
    parser.add_argument("--iterations", type=int, default=50,
                        help="Number of research iterations (default: 50)")
    parser.add_argument("--model", type=str, default="gemma-4-31b-it",
                        help="Google AI Studio model (default: gemma-4-31b-it)")
    args = parser.parse_args()

    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        print("[ERROR] GOOGLE_API_KEY environment variable not set.")
        print("  Get a free key at https://aistudio.google.com/apikey")
        print("  export GOOGLE_API_KEY=AIza...")
        sys.exit(1)

    research_loop(args.iterations, args.model, api_key)

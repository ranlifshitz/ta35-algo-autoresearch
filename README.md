# TA35 Algo Research

Autonomous LLM-driven optimization of a TA35 index trading algorithm, inspired by [Karpathy's autoresearch](https://github.com/karpathy/autoresearch).

## Architecture

```
Algo-machine.py       ← the signal engine (modified by the researcher)
algo-simulator.py     ← evaluation harness: feeds 2yr historical bars → KPIs
auto-researcher.py    ← autonomous optimization loop
program.md            ← researcher instructions (goals, constraints, interface)
results.tsv           ← iteration history log (auto-generated)
research-history/     ← per-iteration code snapshots (auto-generated)
data/                 ← market data CSVs (local only, not in repo)
```

## How it works

1. `algo-simulator.py` loads 2 years of hourly TA35/NDX/USDILS data, computes all technical indicators, and feeds bars **one-by-one** to `Algo-machine.py`
2. `auto-researcher.py` runs the simulator, reads the KPIs, and calls Claude with the **current code + results** (never the raw market data)
3. Claude proposes a focused modification to `Algo-machine.py`
4. The new code is validated (syntax + interface check) and evaluated
5. If the score improves, the code is kept; otherwise reverted
6. Repeat for N iterations

## Scoring

```
Primary  : win rate ≥ 75%   (heavy quadratic penalty below target)
Secondary: maximize compounded P&L over the 2-year backtest
```

## Setup

```bash
# Install dependencies
pip install anthropic pandas numpy

# Set API key
export ANTHROPIC_API_KEY=sk-ant-...

# Place market data CSVs in data/
#   data/ta35/ta35_1h_2y.csv
#   data/ndx/ndx_1h_2y.csv
#   data/usdils/usdils_1h_2y.csv
#   data/calendars/israel_holidays.csv
#   data/calendars/us_holidays.csv
```

## Usage

```bash
# Run simulator only (validate current algo)
python3 algo-simulator.py

# Run autonomous research loop (20 iterations by default)
python3 auto-researcher.py

# Custom iterations / model
python3 auto-researcher.py --iterations 50 --model claude-opus-4-6
```

## Current baseline (unoptimized)

| KPI | Value |
|---|---|
| Total trades | 43 |
| Win rate | 37.2% |
| Compounded P&L | +34.35% |
| Avg hold | 219 hours |

## Design principles (from autoresearch)

- The researcher sees **only** code + KPI results — never raw price data
- Every iteration is logged; best code is snapshotted in `research-history/`
- No human in the loop — runs until budget exhausted or target met
- The same `Algo-machine.py` interface is later used for **real-time trading** (swap the simulator feeder for a live data feed)

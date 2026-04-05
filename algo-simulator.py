"""
Algo Simulator
==============
Loads 2-year historical data (TA35, NDX, USD/ILS, calendar),
computes all technical indicators, then feeds bars one-by-one to
TA35AlgoMachine and evaluates performance.

KPIs reported:
  - Total completed trades
  - Winning trades (TP hit) / Losing trades (SL hit)
  - Win rate %
  - Average P&L per trade %
  - Total and compounded P&L %
  - Best / worst single trade %
  - Average holding period (hours)
"""
import os
import importlib.util
import pandas as pd
from datetime import timedelta

# Load Algo-machine.py (hyphen in filename prevents normal import)
_spec = importlib.util.spec_from_file_location(
    "Algo_machine",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "Algo-machine.py")
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
TA35AlgoMachine = _mod.TA35AlgoMachine

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")

TA35_PATH   = os.path.join(DATA, "ta35",      "ta35_1h_2y.csv")
NDX_PATH    = os.path.join(DATA, "ndx",       "ndx_1h_2y.csv")
USDILS_PATH = os.path.join(DATA, "usdils",    "usdils_1h_2y.csv")
IL_HOL_PATH = os.path.join(DATA, "calendars", "israel_holidays.csv")
US_HOL_PATH = os.path.join(DATA, "calendars", "us_holidays.csv")


# ---------------------------------------------------------------------------
# Data loading & indicator computation
# ---------------------------------------------------------------------------

def load_data():
    print("Loading market data...")

    il_hol = pd.read_csv(IL_HOL_PATH, parse_dates=['date'])
    us_hol = pd.read_csv(US_HOL_PATH, parse_dates=['date'])
    holidays = set(il_hol['date'].dt.date) | set(us_hol['date'].dt.date)

    ta35   = pd.read_csv(TA35_PATH)
    ndx    = pd.read_csv(NDX_PATH)
    usdils = pd.read_csv(USDILS_PATH)

    for df in [ta35, ndx, usdils]:
        df['datetime'] = pd.to_datetime(df['datetime'], utc=True)
        df.set_index('datetime', inplace=True)

    ta35   = ta35.rename(columns={'Open':'ta35_open','High':'ta35_high','Low':'ta35_low','Close':'ta35_close'})
    ndx    = ndx.rename(columns={'Close':'ndx_close'})
    usdils = usdils.rename(columns={'Close':'usdils_close'})

    # Markets trade at different minute-offsets (:50, :30, :00) so exact-timestamp
    # join yields all NaN. Use merge_asof to match each TA35 bar to the most
    # recent NDX / USDILS bar that closed before or at the same moment.
    ta35_reset   = ta35[['ta35_open','ta35_high','ta35_low','ta35_close']].reset_index()
    ndx_reset    = ndx[['ndx_close']].reset_index()
    usdils_reset = usdils[['usdils_close']].reset_index()

    df = pd.merge_asof(ta35_reset.sort_values('datetime'),
                       ndx_reset.sort_values('datetime'),
                       on='datetime', direction='backward')
    df = pd.merge_asof(df.sort_values('datetime'),
                       usdils_reset.sort_values('datetime'),
                       on='datetime', direction='backward')
    df = df.set_index('datetime')

    # ATR (14-period)
    df['prev_close'] = df['ta35_close'].shift(1)
    df['tr'] = pd.concat([
        df['ta35_high'] - df['ta35_low'],
        (df['ta35_high'] - df['prev_close']).abs(),
        (df['ta35_low']  - df['prev_close']).abs(),
    ], axis=1).max(axis=1)
    df['atr'] = df['tr'].rolling(14).mean()

    # Moving averages
    df['ma50']  = df['ta35_close'].rolling(50).mean()
    df['ma150'] = df['ta35_close'].rolling(150).mean()

    # Gap
    df['gap_up'] = (df['ta35_open'] - df['prev_close']) / df['prev_close']

    # RSI (14)
    delta = df['ta35_close'].diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    df['rsi'] = 100 - (100 / (1 + gain / loss))

    # MACD
    ema12 = df['ta35_close'].ewm(span=12, adjust=False).mean()
    ema26 = df['ta35_close'].ewm(span=26, adjust=False).mean()
    df['macd']        = ema12 - ema26
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()

    # Price extrema (pattern detection)
    df['low_40h']  = df['ta35_low'].rolling(40).min()
    df['low_10h']  = df['ta35_low'].rolling(10).min()
    df['high_40h'] = df['ta35_high'].rolling(40).max()

    # Cross-asset correlations
    df['ndx_1d_ret']     = df['ndx_close'].pct_change(periods=24, fill_method=None)
    df['ndx_1h_ret']     = df['ndx_close'].pct_change(periods=1,  fill_method=None)
    df['usdils_24h_vol'] = df['usdils_close'].pct_change(fill_method=None).rolling(24).std()
    df['usdils_24h_ret'] = df['usdils_close'].pct_change(periods=24, fill_method=None)

    df = df.dropna()
    print(f"Indicator computation complete. Bars available: {len(df):,}")
    return df, holidays


# ---------------------------------------------------------------------------
# Holiday proximity helper
# ---------------------------------------------------------------------------

def holiday_approaching(current_date, holidays):
    """Return True if a holiday falls within the next 3 calendar days."""
    return any((current_date + timedelta(days=i)).date() in holidays for i in range(3))


# ---------------------------------------------------------------------------
# Simulation loop
# ---------------------------------------------------------------------------

def run_simulation(df, holidays):
    """Feed bars one-by-one to the algo machine. Returns list of completed trades."""
    algo = TA35AlgoMachine()
    trades = []
    open_trade = None

    print("Running simulation...")
    for current_time, row in df.iterrows():
        result = algo.process_hour(current_time, row, holiday_approaching(current_time, holidays))
        sig = result['signal']

        if sig == "BUY":
            open_trade = {
                'entry_time':  current_time,
                'entry_price': result['price'],
                'sl':          result['SL'],
                'tp':          result['TP'],
                'score':       result['score'],
                'flags':       ", ".join(result['flags']),
            }

        elif sig in ("SELL_TP", "SELL_SL") and open_trade:
            ep = result['price']
            en = open_trade['entry_price']
            trades.append({
                **open_trade,
                'exit_time':   current_time,
                'exit_price':  ep,
                'exit_reason': sig,
                'pnl_pct':     (ep - en) / en * 100,
                'hold_hours':  (current_time - open_trade['entry_time']).total_seconds() / 3600,
            })
            open_trade = None

    # Close any still-open position at last bar price
    if open_trade:
        ep = df.iloc[-1]['ta35_close']
        en = open_trade['entry_price']
        trades.append({
            **open_trade,
            'exit_time':   df.index[-1],
            'exit_price':  ep,
            'exit_reason': 'OPEN_AT_END',
            'pnl_pct':     (ep - en) / en * 100,
            'hold_hours':  (df.index[-1] - open_trade['entry_time']).total_seconds() / 3600,
        })

    return trades


# ---------------------------------------------------------------------------
# KPI reporting
# ---------------------------------------------------------------------------

def print_kpis(trades):
    if not trades:
        print("\nNo completed trades found.")
        return

    df = pd.DataFrame(trades)
    total    = len(df)
    wins     = (df['exit_reason'] == 'SELL_TP').sum()
    losses   = (df['exit_reason'] == 'SELL_SL').sum()
    open_end = (df['exit_reason'] == 'OPEN_AT_END').sum()
    cum_ret  = ((1 + df['pnl_pct'] / 100).prod() - 1) * 100

    print("\n" + "=" * 62)
    print("   ALGO SIMULATOR - PERFORMANCE REPORT")
    print("=" * 62)
    print(f"  Period       : {df['entry_time'].min().date()} -> {df['exit_time'].max().date()}")
    print(f"  Total trades : {total}")
    print(f"    Wins (TP)  : {wins}")
    print(f"    Losses (SL): {losses}")
    print(f"    Open/end   : {open_end}")
    print(f"  Win rate     : {wins / total * 100:.1f}%")
    print(f"  Avg P&L/trade: {df['pnl_pct'].mean():+.2f}%")
    print(f"  Total P&L    : {df['pnl_pct'].sum():+.2f}%")
    print(f"  Compounded   : {cum_ret:+.2f}%")
    print(f"  Best trade   : {df['pnl_pct'].max():+.2f}%")
    print(f"  Worst trade  : {df['pnl_pct'].min():+.2f}%")
    print(f"  Avg hold     : {df['hold_hours'].mean():.1f} hours")
    print("=" * 62)

    print("\n  TRADE LOG")
    print("-" * 62)
    display = df[['entry_time','exit_time','entry_price','exit_price',
                  'exit_reason','pnl_pct','hold_hours','score']].copy()
    display['entry_time'] = display['entry_time'].dt.strftime('%Y-%m-%d %H:%M')
    display['exit_time']  = display['exit_time'].dt.strftime('%Y-%m-%d %H:%M')
    display['pnl_pct']    = display['pnl_pct'].map('{:+.2f}%'.format)
    display['hold_hours'] = display['hold_hours'].map('{:.1f}h'.format)
    print(display.to_string(index=False))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    df, holidays = load_data()
    trades = run_simulation(df, holidays)
    print_kpis(trades)

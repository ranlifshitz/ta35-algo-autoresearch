class TA35AlgoMachine:
    """
    Core signal engine for the TA35 volatility swing correlator.
    Processes one hourly bar at a time and manages position state.

    Input row must contain pre-computed fields:
        ta35_close, atr, ma50, ma150, rsi, macd, macd_signal,
        gap_up, low_40h, low_10h, high_40h,
        ndx_1d_ret, ndx_1h_ret, usdils_24h_vol, usdils_24h_ret

    Call process_hour() once per bar in chronological order.
    """

    def __init__(self, min_score_to_buy=75, atr_sl_mult=2.5, atr_tp_mult=6.0, atr_trail_mult=2.0):
        # Scoring threshold
        self.min_score_to_buy = min_score_to_buy

        # ATR-based risk parameters
        self.atr_sl_mult = atr_sl_mult      # Stop-loss distance in ATR units
        self.atr_tp_mult = atr_tp_mult      # Take-profit distance in ATR units
        self.atr_trail_mult = atr_trail_mult  # Trailing-stop activation in ATR units

        # Live position state
        self.in_position = False
        self.entry_price = 0.0
        self.current_tp = 0.0
        self.current_sl = 0.0

    def reset(self):
        """Clear position state (use between independent simulation runs)."""
        self.in_position = False
        self.entry_price = 0.0
        self.current_tp = 0.0
        self.current_sl = 0.0

    def process_hour(self, current_time, row, is_holiday_approaching: bool) -> dict:
        """
        Evaluate one hourly bar and return a signal dict.

        Returns a dict with keys:
            time, price, signal, score, flags, ATR
            SL and TP are added when in position or on BUY signal.
        """
        ta35_price = row['ta35_close']
        atr_value = row['atr']

        output = {
            "time": current_time,
            "price": ta35_price,
            "signal": "HOLD",
            "score": 0,
            "flags": [],
            "ATR": atr_value,
        }

        # --- Position management: check exits first ---
        if self.in_position:
            if ta35_price >= self.current_tp:
                self.in_position = False
                output['signal'] = "SELL_TP"
                output['SL'] = self.current_sl
                output['TP'] = self.current_tp
                return output

            if ta35_price <= self.current_sl:
                self.in_position = False
                output['signal'] = "SELL_SL"
                output['SL'] = self.current_sl
                output['TP'] = self.current_tp
                return output

            # Trailing stop: move SL to breakeven once price moves atr_trail_mult ATRs in our favour
            if ta35_price >= self.entry_price + (atr_value * self.atr_trail_mult) and self.current_sl < self.entry_price:
                self.current_sl = self.entry_price
                output['flags'].append("TRAILING_STOP_ACTIVATED")

            output['SL'] = self.current_sl
            output['TP'] = self.current_tp
            return output

        # --- Signal scoring (only when flat) ---
        score = 0
        flags = []

        # UTC hours 13-15 = IL morning / US pre-market overlap window
        is_overlap = current_time.hour in [13, 14, 15]
        if row['ndx_1d_ret'] > 0.005 or (is_overlap and row['ndx_1h_ret'] > 0.003):
            score += 20
            flags.append("NDX_BULL")

        if row['usdils_24h_vol'] < 0.005 and row['usdils_24h_ret'] <= 0:
            score += 15
            flags.append("USDILS_STABLE")

        if not is_holiday_approaching:
            score += 10
            flags.append("CLEAR_SCHEDULE")

        if row['ma50'] > row['ma150'] and ta35_price > row['ma50']:
            score += 15
            flags.append("MA_GOLDEN_TREND")

        if 40 < row['rsi'] < 70 and row['macd'] > row['macd_signal']:
            score += 15
            flags.append("MACD_RSI_BULL")

        if row['gap_up'] > 0.003:
            score += 10
            flags.append("GAP_UP_DETECTED")

        handle_depth = (row['high_40h'] - row['low_10h']) / row['high_40h']
        cup_depth = (row['high_40h'] - row['low_40h']) / row['high_40h']
        if cup_depth > handle_depth > 0 and ta35_price >= row['high_40h'] * 0.99:
            score += 15
            flags.append("PATTERN_BREAKOUT_CUP_HS")

        output['score'] = score
        output['flags'] = flags

        # --- Entry: open position if score threshold met ---
        if score >= self.min_score_to_buy:
            self.in_position = True
            self.entry_price = ta35_price
            self.current_sl = ta35_price - (atr_value * self.atr_sl_mult)
            self.current_tp = ta35_price + (atr_value * self.atr_tp_mult)

            output['signal'] = "BUY"
            output['SL'] = self.current_sl
            output['TP'] = self.current_tp

        return output

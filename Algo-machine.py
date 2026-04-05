class TA35AlgoMachine:
    """
    The RSI-Momentum Bridge Engine.
    Builds on 'Trend-Unleashed' by refining the extension filter. 
    It recognizes that in vertical moves, MACD can lag. By adding an 
    alternative extension path—RSI > 75 and positive NASDAQ returns—the 
    algorithm can ride parabolic runs that would otherwise be clipped 
    by the rigid MACD requirement.
    """

    def __init__(self, min_score_to_buy=75, atr_sl_mult=5.0, atr_tp_mult=2.0, atr_trail_mult=3.0):
        self.min_score_to_buy = min_score_to_buy
        self.atr_sl_mult = atr_sl_mult      
        self.atr_tp_mult = atr_tp_mult      
        self.atr_trail_mult = atr_trail_mult 

        self.in_position = False
        self.entry_price = 0.0
        self.current_tp = 0.0
        self.current_sl = 0.0
        self.peak_price = 0.0
        self.is_extended = False
        self.can_extend = False 
        self.soft_tp_mult = 1.5  
        self.lock_mult = 0.5     

    def reset(self):
        """Clear position state."""
        self.in_position = False
        self.entry_price = 0.0
        self.current_tp = 0.0
        self.current_sl = 0.0
        self.peak_price = 0.0
        self.is_extended = False
        self.can_extend = False
        self.soft_tp_mult = 1.5
        self.lock_mult = 0.5

    def process_hour(self, current_time, row, is_holiday_approaching: bool) -> dict:
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

        # --- Position Management ---
        if self.in_position:
            self.peak_price = max(self.peak_price, ta35_price)

            # 1. Hard Take-Profit (The Moon Target)
            if ta35_price >= self.current_tp:
                self.in_position = False
                self.is_extended = False
                output['signal'] = "SELL_TP"
                output['SL'] = self.current_sl
                output['TP'] = self.current_tp
                return output

            # 2. Stop-Loss / Trailing Stop
            if ta35_price <= self.current_sl:
                self.in_position = False
                self.is_extended = False
                output['signal'] = "SELL_TP" if ta35_price > self.entry_price else "SELL_SL"
                output['SL'] = self.current_sl
                output['TP'] = self.current_tp
                return output

            # 3. Extension Transition Logic with RSI-Momentum Bridge
            soft_tp_price = self.entry_price + (atr_value * self.soft_tp_mult)
            
            if ta35_price >= soft_tp_price:
                if self.can_extend and not self.is_extended:
                    # Standard MACD-led momentum
                    macd_momentum = (
                        row['macd'] > row['macd_signal'] and 
                        (row['ndx_1d_ret'] > 0 or row['ndx_1h_ret'] > 0.002)
                    )
                    # RSI-led momentum (The Bridge)
                    rsi_momentum = (
                        row['rsi'] > 75 and 
                        row['ndx_1d_ret'] > 0
                    )
                    
                    if macd_momentum or rsi_momentum:
                        self.is_extended = True
                        self.current_sl = self.entry_price + (atr_value * self.lock_mult)
                        output['flags'].append("EXT_REGIME_ACTIVE")
                        if rsi_momentum and not macd_momentum:
                            output['flags'].append("RSI_BRIDGE_TRIGGERED")
                        self.current_tp = self.entry_price + (atr_value * 25.0)
                    else:
                        # Not enough momentum to justify extension
                        self.in_position = False
                        output['signal'] = "SELL_TP"
                        output['SL'] = self.current_sl
                        output['TP'] = self.current_tp
                        return output
                elif not self.can_extend:
                    self.in_position = False
                    output['signal'] = "SELL_TP"
                    output['SL'] = self.current_sl
                    output['TP'] = self.current_tp
                    return output

            # 4. Trailing Stop Logic
            if self.is_extended:
                trail_sl = self.peak_price - (atr_value * self.atr_trail_mult)
                self.current_sl = max(self.current_sl, trail_sl)
                output['flags'].append("TRAILING_STOP_ACTIVE")
                
                if row['rsi'] > 95:
                    self.in_position = False
                    output['signal'] = "SELL_TP"
                    output['SL'] = self.current_sl
                    output['TP'] = self.current_tp
                    return output
            else:
                if ta35_price >= self.entry_price + (atr_value * self.atr_trail_mult) and self.current_sl < self.entry_price:
                    self.current_sl = self.entry_price
                    output['flags'].append("BREAK_EVEN_STOP")

            output['SL'] = self.current_sl
            output['TP'] = self.current_tp
            return output

        # --- Signal Scoring ---
        score = 0
        flags = []

        if current_time.hour in [7, 8, 9, 13, 14, 15]:
            score += 10
            flags.append("VOLATILITY_WINDOW")

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

        if 45 < row['rsi'] < 70 and row['macd'] > row['macd_signal']:
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

        if ta35_price > row['ma50'] and ta35_price > row['ma150']:
            score += 20
            flags.append("PRICE_ABOVE_MAS")

        output['score'] = score
        output['flags'] = flags

        # --- Triple-Path Entry Logic ---
        is_bullish_candle = ta35_price > row['ta35_open']
        is_above_ma50 = ta35_price > row['ma50']
        is_above_ma150 = ta35_price > row['ma150']

        path_a_triggered = (score >= self.min_score_to_buy and 
                            is_bullish_candle and 
                            is_above_ma50 and 
                            is_above_ma150)

        path_b_triggered = (is_above_ma150 and 
                            row['rsi'] < 35 and 
                            row['ndx_1d_ret'] > 0 and 
                            ta35_price < row['ma50'] and 
                            current_time.hour not in [11, 12])

        path_c_triggered = (is_above_ma150 and 
                            ta35_price < row['ma50'] * 1.002 and 
                            40 <= row['rsi'] < 50 and 
                            is_bullish_candle and 
                            row['macd'] > row['macd_signal'] and 
                            row['ndx_1d_ret'] > 0)

        if path_a_triggered or path_b_triggered or path_c_triggered:
            if row['ma50'] > row['ma150']:
                self.can_extend = True
                output['flags'].append("GOLDEN_REGIME")
            else:
                self.can_extend = True if path_a_triggered else False

            if path_b_triggered:
                output['flags'].append("BULL_DIP_ENTRY")
                tp_mult = 2.0
            elif path_c_triggered:
                output['flags'].append("SQUEEZE_ENTRY")
                tp_mult = 2.3 if score >= 90 else 2.0
            else:
                output['flags'].append("MOMENTUM_ENTRY")
                tp_mult = self.atr_tp_mult
                
            self.in_position = True
            self.is_extended = False
            self.entry_price = ta35_price
            self.peak_price = ta35_price
            self.current_sl = ta35_price - (atr_value * self.atr_sl_mult)
            self.current_tp = ta35_price + (atr_value * tp_mult)

            output['signal'] = "BUY"
            output['SL'] = self.current_sl
            output['TP'] = self.current_tp

        return output
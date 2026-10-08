"""Plays a trade plan forward against real 4h candles, the same way for the
live track record and for the backtest.

Rules (spot, long only):
- A "limit" entry fills only if price trades down to it within `fill_bars`.
- Conservative: if one candle touches both the stop and a target, the stop counts.
- Target 1: sell half and move the stop to the entry price (breakeven).
- Target 2: sell the rest.
- Trend exit: a 4h close below the 20-period average (if enabled).
- Time exit: leave at the close after `max_bars` candles (60 = 10 days).
Results are in R, where 1R is the amount risked (entry minus stop), after fees.
"""
from . import analysis

FEE = 0.001  # Bitget spot fee per side


def manage_trade(c4h: list, start: int, entry: float, stop: float, tp1: float, tp2: float,
                 limit: bool = False, exit_on_ema20: bool = True, max_bars: int = 60,
                 fill_bars: int = 30, e20: list | None = None) -> dict:
    """Plays candles c4h[start+1:] forward. Returns status, result_r (if closed) and bars held."""
    risk = entry - stop
    if risk <= 0:
        return {"status": "invalid"}
    if exit_on_ema20 and e20 is None:
        e20 = analysis.ema([c[4] for c in c4h], 20)
    fee_r = 2 * FEE * entry / risk
    filled = not limit
    fill_i = start if filled else None
    half, banked, cur_stop = False, 0.0, stop
    last_close = entry
    for i in range(start + 1, len(c4h)):
        _ts, _o, high, low, close = c4h[i][:5]
        if not filled:
            if low <= entry:
                filled, fill_i = True, i
            elif i - start >= fill_bars:
                return {"status": "never filled", "result_r": 0.0, "bars": 0, "closed_i": i}
            else:
                continue
        if low <= cur_stop:
            r = banked + (0.5 if half else 1.0) * (cur_stop - entry) / risk
            return _done("target 1 then breakeven" if half else "stopped", r - fee_r, i, fill_i, half)
        if not half and high >= tp1:
            half, banked, cur_stop = True, 0.5 * (tp1 - entry) / risk, entry
        if half and high >= tp2:
            return _done("target 2", banked + 0.5 * (tp2 - entry) / risk - fee_r, i, fill_i, True)
        if exit_on_ema20 and i > fill_i and e20[i] is not None and close < e20[i]:
            return _done("trend exit", banked + (0.5 if half else 1.0) * (close - entry) / risk - fee_r, i, fill_i, half)
        if i - fill_i >= max_bars:
            return _done("time exit", banked + (0.5 if half else 1.0) * (close - entry) / risk - fee_r, i, fill_i, half)
        last_close = close
    status = "open" if filled else "pending"
    out = {"status": status, "bars": (len(c4h) - 1 - fill_i) if filled else 0}
    if filled:
        out["unrealized_r"] = round(banked + (0.5 if half else 1.0) * (last_close - entry) / risk, 2)
        out["half_taken"] = half
    return out


def _done(status: str, r: float, i: int, fill_i: int, half: bool) -> dict:
    return {"status": status, "result_r": round(r, 2), "bars": i - fill_i, "closed_i": i, "half_taken": half}

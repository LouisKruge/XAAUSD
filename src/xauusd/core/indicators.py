"""Vectorised indicators, hand-written.

Hand-written rather than TA-Lib (a C build dependency on Windows) or pandas-ta
(unmaintained): the whole set is ~200 lines, is unit-tested against known vectors, and
never surprises us with a repainting or lookahead quirk we did not write.

CONTRACT: every function returns an array the same length as its input, where element
i is computed ONLY from elements <= i. No function may look forward. Warm-up positions
are NaN, and callers must treat NaN as "not enough history", never as zero.
"""

from __future__ import annotations

import numpy as np

from xauusd.data.series import BarSeries


def _nan(n: int) -> np.ndarray:
    return np.full(n, np.nan, dtype=np.float64)


def sma(values: np.ndarray, period: int) -> np.ndarray:
    n = len(values)
    out = _nan(n)
    if n < period or period < 1:
        return out
    c = np.cumsum(np.insert(values.astype(np.float64), 0, 0.0))
    out[period - 1 :] = (c[period:] - c[:-period]) / period
    return out


def ema(values: np.ndarray, period: int) -> np.ndarray:
    """Seeded with an SMA so the first value is not an arbitrary single observation."""
    n = len(values)
    out = _nan(n)
    if n < period or period < 1:
        return out
    alpha = 2.0 / (period + 1.0)
    out[period - 1] = float(np.mean(values[:period]))
    for i in range(period, n):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def rma(values: np.ndarray, period: int) -> np.ndarray:
    """Wilder's smoothing — what ATR, RSI and ADX are actually defined with."""
    n = len(values)
    out = _nan(n)
    if n < period or period < 1:
        return out
    out[period - 1] = float(np.mean(values[:period]))
    for i in range(period, n):
        out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return out


def true_range(series: BarSeries) -> np.ndarray:
    n = len(series)
    if n == 0:
        return _nan(0)
    tr = np.empty(n, dtype=np.float64)
    tr[0] = series.high[0] - series.low[0]
    if n > 1:
        hl = series.high[1:] - series.low[1:]
        hc = np.abs(series.high[1:] - series.close[:-1])
        lc = np.abs(series.low[1:] - series.close[:-1])
        tr[1:] = np.maximum(hl, np.maximum(hc, lc))
    return tr


def atr(series: BarSeries, period: int = 14) -> np.ndarray:
    return rma(true_range(series), period)


def atr_last(series: BarSeries, period: int = 14) -> float:
    """Most recent ATR, or NaN when there is not enough history.

    Returning NaN rather than a fallback is deliberate: ATR scales every structural
    threshold in the system, so a made-up value would quietly change what counts as a
    valid break of structure.
    """
    if len(series) < period + 1:
        return float("nan")
    return float(atr(series, period)[-1])


def rsi(values: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(values)
    out = _nan(n)
    if n < period + 1:
        return out
    delta = np.diff(values)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = rma(gain, period)
    avg_loss = rma(loss, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.divide(avg_gain, avg_loss, out=np.full_like(avg_gain, np.inf), where=avg_loss > 0)
    out[1:] = 100.0 - (100.0 / (1.0 + rs))
    out[1:][avg_loss == 0] = 100.0
    return out


def macd(
    values: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    line = ema(values, fast) - ema(values, slow)
    valid = ~np.isnan(line)
    sig = _nan(len(values))
    if valid.any():
        first = int(np.argmax(valid))
        sig_valid = ema(line[first:], signal)
        sig[first:] = sig_valid
    return line, sig, line - sig


def adx(series: BarSeries, period: int = 14) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Wilder's ADX with +DI / -DI. Used only for regime classification."""
    n = len(series)
    if n < period * 2:
        return _nan(n), _nan(n), _nan(n)
    up = np.diff(series.high)
    down = -np.diff(series.low)
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = true_range(series)[1:]

    atr_ = rma(tr, period)
    plus_di_v = 100.0 * np.divide(rma(plus_dm, period), atr_, out=np.zeros(n - 1), where=atr_ > 0)
    minus_di_v = 100.0 * np.divide(rma(minus_dm, period), atr_, out=np.zeros(n - 1), where=atr_ > 0)
    denom = plus_di_v + minus_di_v
    dx = 100.0 * np.divide(
        np.abs(plus_di_v - minus_di_v), denom, out=np.zeros(n - 1), where=denom > 0
    )
    adx_v = rma(dx, period)

    out_adx, out_p, out_m = _nan(n), _nan(n), _nan(n)
    out_adx[1:], out_p[1:], out_m[1:] = adx_v, plus_di_v, minus_di_v
    return out_adx, out_p, out_m


def bollinger(
    values: np.ndarray, period: int = 20, std: float = 2.0
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mid = sma(values, period)
    n = len(values)
    dev = _nan(n)
    if n >= period:
        strided = np.lib.stride_tricks.sliding_window_view(values, period)
        dev[period - 1 :] = strided.std(axis=1, ddof=0)
    return mid + std * dev, mid, mid - std * dev


def rolling_vwap(series: BarSeries, period: int = 20) -> np.ndarray:
    """Rolling VWAP on tick volume.

    Note: session-anchored VWAP is the institutionally meaningful one; that is computed
    in the session engine, which knows where the session began. This rolling version is
    only a smoothing reference.
    """
    n = len(series)
    out = _nan(n)
    if n < period:
        return out
    typical = (series.high + series.low + series.close) / 3.0
    vol = series.tick_volume.astype(np.float64)
    pv = typical * vol
    c_pv = np.cumsum(np.insert(pv, 0, 0.0))
    c_v = np.cumsum(np.insert(vol, 0, 0.0))
    num = c_pv[period:] - c_pv[:-period]
    den = c_v[period:] - c_v[:-period]
    out[period - 1 :] = np.divide(num, den, out=np.zeros_like(num), where=den > 0)
    return out


def realized_volatility(closes: np.ndarray, period: int = 20, annualize: int = 0) -> np.ndarray:
    """Std-dev of log returns. `annualize` = periods per year, 0 for raw."""
    n = len(closes)
    out = _nan(n)
    if n < period + 1:
        return out
    with np.errstate(divide="ignore", invalid="ignore"):
        rets = np.diff(np.log(closes))
    strided = np.lib.stride_tricks.sliding_window_view(rets, period)
    vals = strided.std(axis=1, ddof=1)
    if annualize:
        vals = vals * np.sqrt(annualize)
    out[period:] = vals
    return out


def percentile_rank(values: np.ndarray, window: int) -> np.ndarray:
    """Rank of each value within its own trailing window, in [0, 1].

    Strictly backward-looking: position i is ranked against [i-window+1, i] only.
    """
    n = len(values)
    out = _nan(n)
    if n < window or window < 2:
        return out
    strided = np.lib.stride_tricks.sliding_window_view(values, window)
    current = values[window - 1 :]
    out[window - 1 :] = (strided <= current[:, None]).sum(axis=1) / float(window)
    return out


def slope(values: np.ndarray, period: int) -> np.ndarray:
    """Least-squares slope over a trailing window, per bar."""
    n = len(values)
    out = _nan(n)
    if n < period or period < 2:
        return out
    x = np.arange(period, dtype=np.float64)
    x_mean = x.mean()
    x_dev = x - x_mean
    denom = float((x_dev**2).sum())
    strided = np.lib.stride_tricks.sliding_window_view(values, period)
    y_mean = strided.mean(axis=1)
    out[period - 1 :] = (strided - y_mean[:, None]) @ x_dev / denom
    return out


def zscore(values: np.ndarray, window: int) -> np.ndarray:
    n = len(values)
    out = _nan(n)
    if n < window or window < 2:
        return out
    strided = np.lib.stride_tricks.sliding_window_view(values, window)
    mu = strided.mean(axis=1)
    sd = strided.std(axis=1, ddof=1)
    out[window - 1 :] = np.divide(
        values[window - 1 :] - mu, sd, out=np.zeros_like(mu), where=sd > 0
    )
    return out


def last_valid(arr: np.ndarray, default: float = float("nan")) -> float:
    """Most recent non-NaN value."""
    if arr.size == 0:
        return default
    finite = np.isfinite(arr)
    if not finite.any():
        return default
    return float(arr[np.flatnonzero(finite)[-1]])

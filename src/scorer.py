"""L2 技術指標評分系統（滿分 100 分）。

RS（相對強度）維度與美股版不同：台股沒有對應美股 XLK/XLV 等 sector ETF 的完整
體系，改用「同產業（來自 universe.py 的 sector_map）equal-weight 自建籃子」計算
相對報酬，樣本 < 3 支時 fallback 為 ^TWII（見 market.py BENCHMARK_TICKER）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from market import BENCHMARK_TICKER


WEIGHT_MA = 20
WEIGHT_RSI = 18
WEIGHT_MACD = 17
WEIGHT_VOLUME = 15
WEIGHT_MOMENTUM = 15
WEIGHT_RS = 15

L2_TARGET_COUNT = 55
MIN_PEERS_FOR_BASKET = 3


# ── 純 pandas 指標計算 ──────────────────────────────────────────

def _ema(series: pd.Series, span: int) -> float:
    return float(series.ewm(span=span, adjust=False).mean().iloc[-1])


def _calc_rsi(close: pd.Series, length: int = 14) -> float:
    if len(close) < length:
        return float("nan")
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / length, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / length, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi = (100 - 100 / (1 + rs)).dropna()
    return float(rsi.iloc[-1]) if not rsi.empty else float("nan")


def _macd_histogram(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.Series:
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return (macd_line - signal_line).dropna()


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> float:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr_series = tr.ewm(alpha=1 / length, adjust=False).mean().dropna()
    return float(atr_series.iloc[-1]) if not atr_series.empty else float("nan")


# ── 各項評分函式（與美股版邏輯一致，不因市場而異）─────────────

def _score_ma(close: pd.Series) -> float:
    if len(close) < 50:
        return 0.0
    e5, e10, e20, e50 = _ema(close, 5), _ema(close, 10), _ema(close, 20), _ema(close, 50)
    if any(pd.isna(v) for v in [e5, e10, e20, e50]):
        return 0.0
    conditions = [e5 > e10, e10 > e20, e20 > e50]
    return round(WEIGHT_MA * sum(conditions) / len(conditions), 2)


def _score_rsi(rsi: float, regime: str = "") -> float:
    if pd.isna(rsi):
        return 0.0
    if regime == "BULL_TREND":
        if 50 <= rsi <= 80:
            return float(WEIGHT_RSI)
        if rsi > 80 or 40 <= rsi < 50:
            return float(WEIGHT_RSI * 0.5)
        return 0.0
    else:
        if 50 <= rsi <= 70:
            return float(WEIGHT_RSI)
        if (40 <= rsi < 50) or (70 < rsi <= 80):
            return float(WEIGHT_RSI * 0.5)
        return 0.0


def _score_macd(close: pd.Series) -> float:
    if len(close) < 35:
        return 0.0
    hist = _macd_histogram(close)
    if len(hist) < 2:
        return 0.0
    last, prev = float(hist.iloc[-1]), float(hist.iloc[-2])
    if last > 0 and last > prev:
        return float(WEIGHT_MACD)
    if last > 0:
        return float(WEIGHT_MACD * 0.5)
    return 0.0


def _score_volume(df: pd.DataFrame) -> float:
    vol = df["Volume"].dropna()
    if len(vol) < 5:
        return 0.0
    avg30 = float(vol.tail(30).mean()) if len(vol) >= 30 else float(vol.mean())
    today_vol = float(vol.iloc[-1])
    if avg30 == 0:
        return 0.0
    ratio = today_vol / avg30

    close = df["Close"].dropna()
    high = df["High"].dropna()
    low = df["Low"].dropna()
    if len(close) < 1 or len(high) < 1 or len(low) < 1:
        return 0.0
    c, h, l = float(close.iloc[-1]), float(high.iloc[-1]), float(low.iloc[-1])
    k_pos = 0.5 if h == l else (c - l) / (h - l)

    if ratio >= 1.5:
        vtf_base = float(WEIGHT_VOLUME) if k_pos >= 0.6 else 0.0
    elif ratio >= 1.0:
        vtf_base = float(WEIGHT_VOLUME * 0.5) if k_pos >= 0.6 else 0.0
    else:
        vtf_base = 0.0

    if vtf_base == 0.0:
        return 0.0

    vol_tail = vol.values[-5:]
    if len(vol_tail) < 5 or avg30 == 0:
        vol_trend_5d = 0.0
    else:
        slope = np.polyfit(range(5), vol_tail, 1)[0]
        vol_trend_5d = float(np.clip(slope / avg30, -1.0, 1.0))

    if vol_trend_5d > 0.2:
        multiplier = 1.0
    elif vol_trend_5d >= -0.1:
        multiplier = 0.85
    else:
        multiplier = 0.65

    return round(vtf_base * multiplier, 2)


def _score_momentum(df: pd.DataFrame) -> float:
    close = df["Close"].dropna()
    high = df["High"].dropna()
    low = df["Low"].dropna()
    if len(close) < 20 or len(high) < 15 or len(low) < 15:
        return 0.0
    p0, p1 = float(close.iloc[-20]), float(close.iloc[-1])
    if p0 == 0:
        return 0.0
    atr14 = _atr(high, low, close, length=14)
    if pd.isna(atr14) or atr14 <= 0:
        return 0.0
    momentum_20d_atr = (p1 - p0) / atr14

    p5 = float(close.iloc[-5]) if len(close) >= 5 else p0
    momentum_5d_atr = (p1 - p5) / atr14

    if momentum_20d_atr >= 2.0:
        return float(WEIGHT_MOMENTUM) if momentum_5d_atr >= 0.5 else float(WEIGHT_MOMENTUM * 0.5)
    elif momentum_20d_atr >= 1.0:
        return float(WEIGHT_MOMENTUM * 0.5) if momentum_5d_atr >= 0.3 else float(WEIGHT_MOMENTUM * 0.25)
    elif momentum_20d_atr > 0:
        return float(WEIGHT_MOMENTUM * 0.25)
    return 0.0


def _stock_5d_return(df: pd.DataFrame) -> float | None:
    close = df["Close"].dropna()
    if len(close) < 5:
        return None
    start = float(close.iloc[-5])
    if start == 0:
        return None
    return (float(close.iloc[-1]) - start) / start * 100


def _industry_basket_return(
    sym: str,
    sector: str,
    price_data: dict,
    sector_map: dict[str, str],
) -> float | None:
    """同產業 equal-weight 籃子 5 日報酬率，樣本 < MIN_PEERS_FOR_BASKET 時回傳 None（觸發 fallback）。"""
    if not sector:
        return None
    peers = [s for s, sec in sector_map.items() if sec == sector and s != sym and s in price_data]
    if len(peers) < MIN_PEERS_FOR_BASKET:
        return None
    rets = [r for r in (_stock_5d_return(price_data[p]) for p in peers) if r is not None]
    if len(rets) < MIN_PEERS_FOR_BASKET:
        return None
    return sum(rets) / len(rets)


def _calc_rs_score(sym: str, df: pd.DataFrame, sector: str, price_data: dict, sector_map: dict[str, str]) -> float:
    """相對強度：個股 5 日報酬率 − 對照基準 5 日報酬率（同產業籃子，樣本不足 fallback ^TWII）。"""
    stock_ret = _stock_5d_return(df)
    if stock_ret is None:
        return 0.0

    benchmark_ret = _industry_basket_return(sym, sector, price_data, sector_map)
    if benchmark_ret is None:
        twii_df = price_data.get(BENCHMARK_TICKER)
        if twii_df is None or twii_df.empty:
            return 0.0
        benchmark_ret = _stock_5d_return(twii_df)
        if benchmark_ret is None:
            return 0.0

    rs_5d = stock_ret - benchmark_ret
    if rs_5d >= 2.0:
        return float(WEIGHT_RS)
    if rs_5d >= 0.5:
        return 8.0
    if rs_5d >= -0.5:
        return 3.0
    return 0.0


def _is_oversold_reversal_candidate(sym: str, df: pd.DataFrame) -> bool:
    close = df["Close"].dropna()
    if len(close) < 20:
        return False
    rsi_val = _calc_rsi(close)
    if pd.isna(rsi_val) or rsi_val >= 35:
        return False
    p20d = float(close.iloc[-20])
    if p20d == 0:
        return False
    dev_20d = (float(close.iloc[-1]) - p20d) / p20d * 100
    return dev_20d <= -15.0


def score_stock(
    sym: str,
    df: pd.DataFrame,
    regime: str = "",
    sector: str = "",
    price_data: dict | None = None,
    sector_map: dict[str, str] | None = None,
) -> dict:
    close = df["Close"].dropna()
    latest_close = float(close.iloc[-1]) if len(close) > 0 else 0.0

    rsi_val = _calc_rsi(close)

    ma = _score_ma(close)
    rsi = _score_rsi(rsi_val, regime=regime)
    macd = _score_macd(close)
    vol = _score_volume(df)
    mom = _score_momentum(df)
    rs = _calc_rs_score(sym, df, sector, price_data, sector_map or {}) if price_data is not None else 0.0

    return {
        "symbol": sym,
        "price": latest_close,
        "sector": sector,
        "total_score": round(ma + rsi + macd + vol + mom + rs, 2),
        "ma_score": ma,
        "rsi_score": rsi,
        "macd_score": macd,
        "volume_score": vol,
        "momentum_score": mom,
        "rs_score": rs,
    }


def score_all(
    symbols: list[str],
    price_data: dict[str, pd.DataFrame],
    min_score: float = 60.0,
    regime: str = "",
    sector_map: dict[str, str] | None = None,
) -> list[dict]:
    sector_map = sector_map or {}
    results = []
    for sym in symbols:
        if sym in price_data and len(price_data[sym]) >= 20:
            sector = sector_map.get(sym, "")
            results.append(score_stock(sym, price_data[sym], regime=regime, sector=sector, price_data=price_data, sector_map=sector_map))

    if regime == "PANIC_REVERSAL":
        effective_min = 40.0
    elif regime == "CONSOLIDATION_VOLATILE":
        effective_min = max(min_score, 65.0)
    else:
        effective_min = min_score

    force_pass: set[str] = set()
    if regime == "PANIC_REVERSAL":
        for sym in symbols:
            if sym in price_data and _is_oversold_reversal_candidate(sym, price_data[sym]):
                force_pass.add(sym)
        if force_pass:
            print(f"[scorer] PANIC_REVERSAL 強制放行 {len(force_pass)} 支超賣反轉候選股")

    qualified = sorted(
        [r for r in results if r["total_score"] >= effective_min or r["symbol"] in force_pass],
        key=lambda x: x["total_score"],
        reverse=True,
    )

    if len(qualified) > L2_TARGET_COUNT:
        cutoff_score = qualified[L2_TARGET_COUNT - 1]["total_score"]
        candidates = [r for r in qualified if r["total_score"] >= cutoff_score or r["symbol"] in force_pass]
    else:
        candidates = qualified

    print(f"[scorer] L2 評分：{len(symbols)} 支 → {len(candidates)} 支候選（門檻 {effective_min:.0f} 分，Regime={regime or 'N/A'}）")
    return candidates

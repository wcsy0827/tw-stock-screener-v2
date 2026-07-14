"""大盤 Regime 快速判定：市場廣度 + 加權指數已實現波動率（HV20，VIX 替代）。

MVP 已知限制（見 README「未解決的設計問題」）：
- 台股沒有現成的前瞻隱含波動率指數（VIX 等價物），改用 ^TWII 20日已實現波動率（HV20，
  年化）替代。HV 是落後指標（用過去報酬率算），VIX 是前瞻指標，語意不同。

HV_LOW_THRESHOLD / HV_HIGH_THRESHOLD 校準方式（見 scripts/calibrate_hv.py）：
不照抄美股 VIX 20/25 的絕對數值，而是對齊「Regime 出現頻率」——美股版 VIX<20 大約
覆蓋七成交易日、VIX>=25 大約一成，取 ^TWII HV20 歷史分布的第 70 / 90 百分位。
校準窗：2021-07-14 ~ 2026-07-14（5年，1194 個 HV20 樣本）。若要重新校準，直接重跑
scripts/calibrate_hv.py 並更新下方數值＋此註解的日期窗。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import yfinance as yf

BENCHMARK_TICKER = "^TWII"
BREADTH_SMOOTHING_DAYS = 3

# 已校準（見上方模組說明），非美股 VIX 數值移植
HV_LOW_THRESHOLD = 19.44   # P70
HV_HIGH_THRESHOLD = 27.49  # P90

_BREADTH_EXCLUDED: frozenset[str] = frozenset({BENCHMARK_TICKER})


# ── 市場廣度計算（與美股版邏輯對稱）──────────────────────────────

def calculate_market_breadth(
    all_stocks_data: dict,
    smoothing_days: int = BREADTH_SMOOTHING_DAYS,
) -> float:
    """計算市場廣度（收盤 > 50 SMA 比例），回傳近 smoothing_days 日算術平均。"""
    def _breadth_for_offset(offset: int) -> float | None:
        above, total = 0, 0
        for sym, df in all_stocks_data.items():
            if sym in _BREADTH_EXCLUDED:
                continue
            close = df["Close"].dropna()
            if len(close) < 50 + offset:
                continue
            effective = close.iloc[: len(close) - offset] if offset else close
            sma50 = float(effective.tail(50).mean())
            total += 1
            if float(effective.iloc[-1]) > sma50:
                above += 1
        if total == 0:
            return None
        pct = round(above / total * 100, 1)
        if offset == 0:
            print(f"[market] 市場廣度：{above}/{total} 支股票站上50SMA = {pct}%（今日）")
        return pct

    values = []
    for d in range(smoothing_days):
        v = _breadth_for_offset(d)
        if v is not None:
            values.append(v)

    if not values:
        return 50.0
    avg = round(sum(values) / len(values), 1)
    if smoothing_days > 1:
        print(f"[market] 市場廣度 {smoothing_days}日均：{avg}%（用於 Regime 判定）")
    return avg


def _twii_hv20(close: pd.Series) -> float:
    """20日已實現波動率（年化，百分比）——VIX 替代指標，語意為落後指標，見模組說明。"""
    if len(close) < 21:
        return float("nan")
    log_ret = np.log(close / close.shift(1)).dropna()
    hv20 = float(log_ret.tail(20).std() * np.sqrt(252) * 100)
    return hv20


def fetch_twii_hv20(all_stocks_data: dict | None = None) -> tuple[float, bool]:
    """回傳 (hv20, ok)。優先複用 all_stocks_data 中已下載的 ^TWII，缺失才單獨下載。"""
    df = (all_stocks_data or {}).get(BENCHMARK_TICKER)
    if df is None or df.empty:
        try:
            df = yf.download(
                BENCHMARK_TICKER, period="90d", interval="1d",
                auto_adjust=True, progress=False,
            )
        except Exception as e:
            print(f"[market] ^TWII 下載失敗，HV20 使用預設值：{e}")
            return 20.0, False

    close_col = df["Close"] if "Close" in df.columns else pd.Series(dtype=float)
    close = (close_col.squeeze() if isinstance(close_col, pd.DataFrame) else close_col).dropna()
    hv20 = _twii_hv20(close)
    if pd.isna(hv20):
        return 20.0, False
    return round(hv20, 2), True


# ── 五象限 Regime 判定（VIX → HV20 替代，邊界已校準見模組說明）──────

def determine_market_regime(breadth_pct: float, hv20: float) -> dict:
    """
    五象限分類矩陣（架構對稱美股版，HV20 邊界已用歷史分布校準，見模組說明）：
      breadth >= 60% + HV20 < HV_LOW_THRESHOLD   → BULL_TREND
      breadth 35~60% + HV20 < HV_LOW_THRESHOLD   → CONSOLIDATION
      breadth 35~60% + HV20 >= HV_LOW_THRESHOLD  → CONSOLIDATION_VOLATILE
      breadth < 35% + HV20 >= HV_HIGH_THRESHOLD  → PANIC_REVERSAL
      breadth < 35% + HV20 < HV_HIGH_THRESHOLD   → BEAR_DISTRIBUTION
    """
    if breadth_pct >= 60 and hv20 < HV_LOW_THRESHOLD:
        return {"regime": "BULL_TREND", "primary_strategy": "動能策略"}
    elif breadth_pct >= 35 and hv20 < HV_LOW_THRESHOLD:
        return {"regime": "CONSOLIDATION", "primary_strategy": "突破策略（積極）"}
    elif breadth_pct >= 35:
        return {"regime": "CONSOLIDATION_VOLATILE", "primary_strategy": "突破策略（保守）"}
    elif hv20 >= HV_HIGH_THRESHOLD:
        return {"regime": "PANIC_REVERSAL", "primary_strategy": "反轉策略"}
    else:
        return {"regime": "BEAR_DISTRIBUTION", "primary_strategy": ""}


def fetch_regime_quick(all_stocks_data: dict) -> tuple[str, float, float, bool]:
    """回傳 (regime, breadth_pct, hv20, hv_ok)。"""
    breadth_pct = calculate_market_breadth(all_stocks_data)
    hv20, hv_ok = fetch_twii_hv20(all_stocks_data)
    regime_dict = determine_market_regime(breadth_pct, hv20)
    regime = regime_dict["regime"]

    hv_status = f"HV20={hv20:.1f}%" if hv_ok else "HV20=20.0%（fallback）"
    print(f"[market] Regime：{regime}（廣度={breadth_pct}%，{hv_status}，主推策略：{regime_dict['primary_strategy'] or '全面防禦'}）")
    return regime, breadth_pct, hv20, hv_ok

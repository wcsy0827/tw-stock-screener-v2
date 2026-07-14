"""大盤 Regime 快速判定：市場廣度 + 波動率（優先真 VIX，缺失 fallback ^TWII HV20）。

波動率訊號來源（見 fetch_regime_quick 的 vix_source 回傳值）：
- "taifex"：TAIFEX 臺指選擇權波動率指數（見 taifex_vix.py），選擇權隱含波動率，
  語意與美股 VIX 相同（前瞻指標）。**已知限制**：TAIFEX 該 endpoint 只保留約
  3~4 個月近期資料，不是深度歷史 archive，無法比照 ^TWII 做長期分位數校準。
- "hv20_fallback"：TAIFEX 抓取失敗時的備援，^TWII 20日已實現波動率（年化），
  是落後指標（用過去報酬率算），語意與 VIX 不同。

VOL_LOW_THRESHOLD / VOL_HIGH_THRESHOLD 校準方式（見 scripts/calibrate_hv.py）：
因為 TAIFEX 真 VIX 歷史尚淺（約4個月）不足以獨立校準分位數，暫時沿用 ^TWII
HV20 的校準值（兩者都是「年化波動率百分比」，量級可比）。校準窗：
2021-07-14 ~ 2026-07-14（5年，1194 個 HV20 樣本），對齊「Regime 出現頻率」
——美股版 VIX<20 大約覆蓋七成交易日、VIX>=25 大約一成，取 HV20 歷史分布的
第 70 / 90 百分位。**待辦（見 TODO.md）**：累積 6~12 個月
`data/taifex_vix_history.json` 後，改用真 VIX 自己的分布重新校準，不要
繼續沿用 HV20 校準值。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import yfinance as yf

import taifex_vix

BENCHMARK_TICKER = "^TWII"
BREADTH_SMOOTHING_DAYS = 3

# 暫時沿用 HV20 校準值（見上方模組說明），非美股 VIX 數值移植
VOL_LOW_THRESHOLD = 19.44   # P70
VOL_HIGH_THRESHOLD = 27.49  # P90

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
    """20日已實現波動率（年化，百分比）——VIX fallback 指標，語意為落後指標，見模組說明。"""
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


def fetch_volatility_signal(all_stocks_data: dict | None = None) -> tuple[float, str]:
    """
    回傳 (vol_value, vix_source)。優先抓 TAIFEX 真 VIX（"taifex"），失敗則
    fallback 為 ^TWII HV20（"hv20_fallback"），兩者共用同一組 Regime 邊界。
    """
    taifex_value = taifex_vix.fetch_latest_vix()
    if taifex_value is not None:
        return round(taifex_value, 2), "taifex"

    hv20, hv_ok = fetch_twii_hv20(all_stocks_data)
    return hv20, "hv20_fallback"


# ── 五象限 Regime 判定（波動率邊界已校準，見模組說明）───────────

def determine_market_regime(breadth_pct: float, vol_value: float) -> dict:
    """
    五象限分類矩陣（架構對稱美股版，波動率邊界已校準，見模組說明）：
      breadth >= 60% + vol < VOL_LOW_THRESHOLD    → BULL_TREND
      breadth 35~60% + vol < VOL_LOW_THRESHOLD    → CONSOLIDATION
      breadth 35~60% + vol >= VOL_LOW_THRESHOLD   → CONSOLIDATION_VOLATILE
      breadth < 35% + vol >= VOL_HIGH_THRESHOLD   → PANIC_REVERSAL
      breadth < 35% + vol < VOL_HIGH_THRESHOLD    → BEAR_DISTRIBUTION
    """
    if breadth_pct >= 60 and vol_value < VOL_LOW_THRESHOLD:
        return {"regime": "BULL_TREND", "primary_strategy": "動能策略"}
    elif breadth_pct >= 35 and vol_value < VOL_LOW_THRESHOLD:
        return {"regime": "CONSOLIDATION", "primary_strategy": "突破策略（積極）"}
    elif breadth_pct >= 35:
        return {"regime": "CONSOLIDATION_VOLATILE", "primary_strategy": "突破策略（保守）"}
    elif vol_value >= VOL_HIGH_THRESHOLD:
        return {"regime": "PANIC_REVERSAL", "primary_strategy": "反轉策略"}
    else:
        return {"regime": "BEAR_DISTRIBUTION", "primary_strategy": ""}


def fetch_regime_quick(all_stocks_data: dict) -> tuple[str, float, float, str]:
    """回傳 (regime, breadth_pct, vol_value, vix_source)。"""
    breadth_pct = calculate_market_breadth(all_stocks_data)
    vol_value, vix_source = fetch_volatility_signal(all_stocks_data)
    regime_dict = determine_market_regime(breadth_pct, vol_value)
    regime = regime_dict["regime"]

    print(f"[market] Regime：{regime}（廣度={breadth_pct}%，波動率={vol_value:.2f}%[{vix_source}]，主推策略：{regime_dict['primary_strategy'] or '全面防禦'}）")
    return regime, breadth_pct, vol_value, vix_source

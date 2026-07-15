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

    ai_prompt_hint 供 ranker.py 的 L3 AI Prompt 直接引用（見 fetch_market_context）。
    """
    if breadth_pct >= 60 and vol_value < VOL_LOW_THRESHOLD:
        return {
            "regime": "BULL_TREND", "primary_strategy": "動能策略",
            "ai_prompt_hint": (
                f"目前大盤環境為【強勢牛市】，市場廣度極佳（{breadth_pct}% 個股站上50SMA），"
                f"整體結構健康。請嚴格執行【動能策略】，優先選擇同產業籃子領先者與均線多頭排列"
                f"之強勢標的，忽略左側反轉訊號。"
            ),
        }
    elif breadth_pct >= 35 and vol_value < VOL_LOW_THRESHOLD:
        return {
            "regime": "CONSOLIDATION", "primary_strategy": "突破策略（積極）",
            "ai_prompt_hint": (
                f"目前大盤環境為【震盪整理】，市場廣度中性（{breadth_pct}% 個股站上50SMA），"
                f"波動率={vol_value:.1f}% 正常。請執行【突破策略（積極型）】，優先選帶量突破"
                f"關鍵壓力位的個股，確認訊號後積極進場。"
            ),
        }
    elif breadth_pct >= 35:
        return {
            "regime": "CONSOLIDATION_VOLATILE", "primary_strategy": "突破策略（保守）",
            "ai_prompt_hint": (
                f"目前大盤環境為【高波動整理】，市場廣度中性（{breadth_pct}% 個股站上50SMA），"
                f"波動率={vol_value:.1f}% 偏高。請執行【突破策略（保守型）】，要求 VTF_Score "
                f">= 2.0、MACD POS_INC 且 RSI 50~65 才考慮進場；訊號不明確一律跳過。"
            ),
        }
    elif vol_value >= VOL_HIGH_THRESHOLD:
        return {
            "regime": "PANIC_REVERSAL", "primary_strategy": "反轉策略",
            "ai_prompt_hint": (
                f"目前大盤環境為【恐慌超跌】，市場廣度偏低（{breadth_pct}% 個股站上50SMA），"
                f"波動率={vol_value:.1f}% 恐慌情緒高。請執行【反轉策略】，尋找非理性殺低、"
                f"靠近長期支撐且出現底背離訊號的個股，嚴設止損，控制倉位。"
            ),
        }
    else:
        return {
            "regime": "BEAR_DISTRIBUTION", "primary_strategy": "",
            "ai_prompt_hint": (
                f"目前大盤環境為【陰跌熊市】，市場廣度極低（{breadth_pct}% 個股站上50SMA），"
                f"波動率={vol_value:.1f}%。風險極高，系統啟動全面防禦，禁止建立新倉位，"
                f"請勿輸出任何買入建議，直接回傳空的 selections 陣列。"
            ),
        }


def fetch_regime_quick(all_stocks_data: dict) -> tuple[str, float, float, str]:
    """回傳 (regime, breadth_pct, vol_value, vix_source)。"""
    breadth_pct = calculate_market_breadth(all_stocks_data)
    vol_value, vix_source = fetch_volatility_signal(all_stocks_data)
    regime_dict = determine_market_regime(breadth_pct, vol_value)
    regime = regime_dict["regime"]

    print(f"[market] Regime：{regime}（廣度={breadth_pct}%，波動率={vol_value:.2f}%[{vix_source}]，主推策略：{regime_dict['primary_strategy'] or '全面防禦'}）")
    return regime, breadth_pct, vol_value, vix_source


# ── 大盤背景（供 ranker.py L3 Prompt 與 publisher.py 儀表板使用）───────

def _trend_label(chg_5d: float) -> str:
    if chg_5d > 1.0:
        return "強勢上漲"
    if chg_5d > 0.3:
        return "溫和上漲"
    if chg_5d > -0.3:
        return "盤整"
    if chg_5d > -1.0:
        return "溫和下跌"
    return "明顯下跌"


def _vol_label(vol_value: float) -> str:
    """波動率五級標籤，門檻依 VOL_LOW_THRESHOLD/VOL_HIGH_THRESHOLD 等比例劃分
    （非美股版硬編碼的 15/20/25/30，因 TW 用校準後的百分位邊界，見模組說明）。"""
    if vol_value < VOL_LOW_THRESHOLD * 0.7:
        return "低恐慌（市場樂觀）"
    if vol_value < VOL_LOW_THRESHOLD:
        return "正常"
    if vol_value < (VOL_LOW_THRESHOLD + VOL_HIGH_THRESHOLD) / 2:
        return "輕微恐慌"
    if vol_value < VOL_HIGH_THRESHOLD:
        return "中度恐慌"
    return "高度恐慌（避險情緒濃厚）"


def _analyze(df: pd.DataFrame | None) -> dict:
    """從 OHLCV DataFrame 計算走勢摘要（供 ^TWII 大盤背景使用）。資料異常時回傳
    {}，不得拋錯拖垮整個大盤背景。"""
    if df is None or df.empty or "Close" not in df.columns:
        return {}
    close = df["Close"].dropna()
    if len(close) < 5:
        return {}

    price = float(close.iloc[-1])
    chg_5d = (price - float(close.iloc[-5])) / float(close.iloc[-5]) * 100 if len(close) >= 5 else 0.0
    chg_20d = (price - float(close.iloc[-20])) / float(close.iloc[-20]) * 100 if len(close) >= 20 else 0.0
    ema20_val = float(close.ewm(span=20, adjust=False).mean().iloc[-1]) if len(close) >= 20 else float("nan")

    result: dict = {
        "price": round(price, 2),
        "change_5d_pct": round(chg_5d, 2),
        "change_20d_pct": round(chg_20d, 2),
        "trend_5d": _trend_label(chg_5d),
    }
    if not pd.isna(ema20_val):
        result["above_ema20"] = price > ema20_val
    return result


def _industry_basket_analysis(
    sector: str,
    sector_map: dict[str, str],
    price_data: dict,
    min_peers: int = 3,
) -> dict:
    """同產業 equal-weight 籃子的 5日/20日平均報酬率（取代美股版 sector ETF，
    台股沒有對應的完整 sector ETF 體系，見 scorer.py 模組說明）。
    樣本 < min_peers 時回傳 {}（不足以代表產業趨勢）。"""
    peers = [s for s, sec in sector_map.items() if sec == sector and s in price_data]
    if len(peers) < min_peers:
        return {}

    rets_5d: list[float] = []
    rets_20d: list[float] = []
    for sym in peers:
        df = price_data.get(sym)
        if df is None:
            continue
        close = df["Close"].dropna()
        if len(close) >= 5 and float(close.iloc[-5]) != 0:
            rets_5d.append((float(close.iloc[-1]) - float(close.iloc[-5])) / float(close.iloc[-5]) * 100)
        if len(close) >= 20 and float(close.iloc[-20]) != 0:
            rets_20d.append((float(close.iloc[-1]) - float(close.iloc[-20])) / float(close.iloc[-20]) * 100)

    if len(rets_5d) < min_peers:
        return {}

    chg_5d = sum(rets_5d) / len(rets_5d)
    result: dict = {
        "change_5d_pct": round(chg_5d, 2),
        "trend_5d": _trend_label(chg_5d),
        "peer_count": len(rets_5d),
    }
    if len(rets_20d) >= min_peers:
        result["change_20d_pct"] = round(sum(rets_20d) / len(rets_20d), 2)
    return result


def fetch_market_context(
    all_stocks_data: dict | None = None,
    sector_map: dict[str, str] | None = None,
    candidate_sectors: set[str] | None = None,
    breadth_pct: float | None = None,
    vol_value: float | None = None,
    vix_source: str | None = None,
) -> dict:
    """
    組裝 L3 AI Prompt 與 publisher 儀表板所需的大盤背景，複用 Step 2/2.5 已下載/
    已計算的資料，不重複下載。

    all_stocks_data:   已下載的全 universe 日K字典（含 ^TWII），供 index/產業籃子分析。
    sector_map:        {symbol: 產業中文名稱}，供產業籃子分組。
    candidate_sectors: 候選股涵蓋的產業集合，只分析相關產業；傳 None 則分析全部。
    breadth_pct/vol_value/vix_source: Step 2.5 已計算的值，有值時直接複用不重算。

    回傳結構：
      {
        "index":   {...}（^TWII 走勢），
        "vix":     {"value":, "label":, "source":},
        "sectors": {"半導體業": {...}, ...}（equal-weight 籃子，取代美股版 ETF），
        "market_breadth_pct": 68.5,
        "regime": "BULL_TREND",
        "primary_strategy": "動能策略",
        "ai_prompt_hint": "...",
      }
    """
    all_stocks_data = all_stocks_data or {}
    sector_map = sector_map or {}
    context: dict = {}

    twii = _analyze(all_stocks_data.get(BENCHMARK_TICKER))
    if twii:
        context["index"] = twii

    vol_final = vol_value if vol_value is not None else 20.0
    context["vix"] = {
        "value": round(vol_final, 2),
        "label": _vol_label(vol_final),
        "source": vix_source or "hv20_fallback",
    }

    context["sectors"] = {}
    sectors_to_check = candidate_sectors if candidate_sectors is not None else set(sector_map.values())
    for sector in sectors_to_check:
        if not sector or sector == "Unknown":
            continue
        data = _industry_basket_analysis(sector, sector_map, all_stocks_data)
        if data:
            context["sectors"][sector] = data

    if breadth_pct is not None:
        regime_info = determine_market_regime(breadth_pct, vol_final)
        context["market_breadth_pct"] = breadth_pct
        context.update(regime_info)

    ok_sectors = len(context.get("sectors", {}))
    print(
        f"[market] 大盤背景：TWII={'ok' if 'index' in context else 'fail'}，"
        f"波動率={'ok' if 'vix' in context else 'fail'}，產業籃子={ok_sectors}個"
    )
    return context

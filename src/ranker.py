"""L3 AI 排序：用 DeepSeek 對 L2 候選股做橫向比較，輸出 Top N。

移植自 `D:\\us-stock-screener\\src\\ranker.py`，主要調整：
- RS_vs_Sector 改用 scorer.py 既有的「同產業 equal-weight 籃子」計算（台股無對應
  美股 sector ETF 體系），不重算，直接複用 `scorer._industry_basket_return`。
- Beta_60D 對照基準由 SPY 改為 ^TWII（`market.BENCHMARK_TICKER`）。
- 移除財報剩餘天數（Earnings_Days_Left）維度：TWSE 未接入財報日曆資料源，
  維持恆定值（例如恆為 99/安全）的欄位對 AI 判斷無資訊量，與其移植一個永遠
  常數的死欄位，不如整個維度先不做，待未來有 TWSE 財報日曆資料源時再補上。
- 幣別由 "$" 改為 "NT$"；持有天數單位改為「台股交易日」；分析師人設改為台股。
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from openai import OpenAI

import market
from scorer import _industry_basket_return, _stock_5d_return

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"
MAX_CANDIDATES_TO_AI = 40   # 最多送給 AI 的候選股數量（已按 L2 分排序，取前 N）
MAX_SECTOR_CANDIDATES = 8   # 每產業最多保留支數（_diversify_candidates）
MAX_RETRIES = 3

# CONSOLIDATION_VOLATILE Regime 的額外 Prompt 指引
_REGIME_EXTRA_HINT: dict[str, str] = {
    "CONSOLIDATION_VOLATILE": (
        "⚠️ 注意：當前為【高波動整理期】，策略應採保守突破，"
        "要求更顯著的確認訊號（VTF_Score >= 2.0、MACD POS_INC、RSI 在 50~65 之間），"
        "訊號不夠明確的標的一律跳過。"
    ),
}

_CACHE_DIR = Path(__file__).parent.parent / ".cache"
_AI_HINTS_PATH = Path(__file__).parent.parent / "data" / "ai_hints.json"


def _ranked_cache_path(market_date: str) -> Path:
    date_str = market_date.replace("-", "")
    return _CACHE_DIR / f"ranked_{date_str}.json"


def _load_ai_hints() -> list[str]:
    """讀取歷史績效回饋行（供未來 analyzer.py 產出時使用）。任何失敗靜默回傳空清單。"""
    try:
        with open(_AI_HINTS_PATH, encoding="utf-8") as f:
            lines = json.load(f).get("prompt_lines", [])
        return [str(x) for x in lines] if isinstance(lines, list) else []
    except Exception:
        return []


# ── 指標計算（純 pandas / numpy）────────────────────────────────

def _ema(series: pd.Series, span: int) -> float:
    return float(series.ewm(span=span, adjust=False).mean().iloc[-1])


def _rsi(series: pd.Series, period: int = 14) -> float:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi = (100 - 100 / (1 + rs)).dropna()
    return float(rsi.iloc[-1]) if not rsi.empty else float("nan")


def _macd_hist(series: pd.Series) -> float:
    ema12 = series.ewm(span=12, adjust=False).mean()
    ema26 = series.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    hist = (macd - signal).dropna()
    return float(hist.iloc[-1]) if not hist.empty else float("nan")


def compute_indicators(
    sym: str,
    df: pd.DataFrame,
    r_twii_global: pd.Series | None = None,
) -> dict:
    """
    計算單支股票指標供 Markdown 表格使用。
    r_twii_global 由呼叫端預計算一次傳入（不在函式內重複計算），None 時 beta_60d 回傳 None。
    """
    close = df["Close"].dropna()
    volume = df["Volume"].dropna()
    high = df["High"].dropna()
    low = df["Low"].dropna()

    price_now  = float(close.iloc[-1])
    price_prev = float(close.iloc[-2])  if len(close) >= 2  else price_now
    price_5d   = float(close.iloc[-5])  if len(close) >= 5  else price_now
    price_20d  = float(close.iloc[-20]) if len(close) >= 20 else price_now

    change_1d = (price_now - price_prev) / price_prev * 100 if price_prev else 0.0
    change_5d = (price_now - price_5d)   / price_5d   * 100 if price_5d   else 0.0

    # EMA
    ema5  = _ema(close, 5)  if len(close) >= 5  else float("nan")
    ema10 = _ema(close, 10) if len(close) >= 10 else float("nan")
    ema20 = _ema(close, 20) if len(close) >= 20 else float("nan")
    ema50 = _ema(close, 50) if len(close) >= 50 else float("nan")
    rsi   = _rsi(close)     if len(close) >= 14 else float("nan")

    # VTF_Score（量能推進因子）
    avg_vol_30 = float(volume.tail(30).mean()) if len(volume) >= 30 else (float(volume.mean()) if len(volume) > 0 else 0.0)
    vol_ratio  = float(volume.iloc[-1]) / avg_vol_30 if avg_vol_30 > 0 else 1.0
    h_last = float(high.iloc[-1]) if len(high) >= 1 else price_now
    l_last = float(low.iloc[-1])  if len(low)  >= 1 else price_now
    k_pos = 0.5 if h_last == l_last else (price_now - l_last) / (h_last - l_last)
    vtf_score: float | None = round(max(-5.0, vol_ratio * (2 * k_pos - 1)), 2)

    # Vol_vs_5DAvg（動能策略回檔量縮確認）
    avg_vol_5 = float(volume.tail(5).mean()) if len(volume) >= 5 else avg_vol_30
    vol_vs_5d_avg = float(volume.iloc[-1]) / avg_vol_5 if avg_vol_5 > 0 else 1.0

    # Vol_vs_20DAvg（突破策略攻擊量確認）
    avg_vol_20 = float(volume.tail(20).mean()) if len(volume) >= 20 else avg_vol_30
    vol_vs_20d_avg = float(volume.iloc[-1]) / avg_vol_20 if avg_vol_20 > 0 else 1.0

    # ATR14
    atr14: float | None = None
    if len(close) >= 15 and len(high) >= 15 and len(low) >= 15:
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr_series = tr.ewm(alpha=1 / 14, adjust=False).mean().dropna()
        if not atr_series.empty:
            atr14 = float(atr_series.iloc[-1])

    # Momentum_ATR（ATR 標準化 20 日動能）
    momentum_atr: float | None = None
    if len(close) >= 20 and atr14 is not None and atr14 > 0:
        momentum_atr = round((price_now - price_20d) / atr14, 2)

    # Beta_60D（需 r_twii_global）
    beta_60d: float | None = None
    if r_twii_global is not None and len(close) >= 31:
        r_stock = close.pct_change().dropna()
        r_s_aln, r_twii_aln = r_stock.align(r_twii_global, join="inner")
        clean = pd.concat([r_s_aln, r_twii_aln], axis=1).dropna()
        clean_60 = clean.tail(60)
        if len(clean_60) >= 30:
            cov = np.cov(clean_60.iloc[:, 0], clean_60.iloc[:, 1])
            if cov[1, 1] != 0:
                beta_60d = round(float(cov[0, 1] / cov[1, 1]), 2)

    # Strategy_Tag 所需欄位
    macd_h = _macd_hist(close) if len(close) >= 35 else float("nan")
    high_20d = float(df["High"].tail(20).max()) if len(df) >= 20 else price_now
    low_20d  = float(df["Low"].tail(20).min())  if len(df) >= 20 else price_now
    dist_from_20d_high_pct = round((price_now - high_20d) / high_20d * 100, 2) if high_20d else 0.0
    rsi_5d_ago = _rsi(close.iloc[:-5]) if len(close) >= 19 else float("nan")
    low14  = float(df["Low"].tail(14).min())  if len(df) >= 14 else price_now
    high14 = float(df["High"].tail(14).max()) if len(df) >= 14 else price_now
    stoch_k = round((price_now - low14) / (high14 - low14) * 100, 1) if high14 != low14 else 50.0
    dist_from_ema50_pct = round((price_now - ema50) / ema50 * 100, 2) if (ema50 == ema50 and ema50) else None

    def _fmt(v: float, decimals: int = 2) -> float | None:
        return None if (v != v) else round(v, decimals)  # NaN check

    return {
        "symbol": sym,
        "price": round(price_now, 2),
        "change_1d_pct":  round(change_1d, 2),
        "change_5d_pct":  round(change_5d, 2),
        "ema5": _fmt(ema5),
        "ema10": _fmt(ema10),
        "ema20": _fmt(ema20),
        "ema50": _fmt(ema50),
        "rsi": _fmt(rsi),
        "macd_hist": _fmt(macd_h, 4),
        "vtf_score": vtf_score,
        "momentum_atr": momentum_atr,
        "atr14": round(atr14, 2) if atr14 is not None else None,
        "beta_60d": beta_60d,
        "vol_vs_5d_avg": round(vol_vs_5d_avg, 2),
        "vol_vs_20d_avg": round(vol_vs_20d_avg, 2),
        # 供 _strategy_tag 使用
        "volume_ratio": round(vol_ratio, 2),
        "high_20d": round(high_20d, 2),
        "low_20d":  round(low_20d, 2),
        "dist_from_20d_high_pct": dist_from_20d_high_pct,
        "rsi_5d_ago":          _fmt(rsi_5d_ago),
        "stoch_k":             stoch_k,
        "dist_from_ema50_pct": dist_from_ema50_pct,
    }


# ── Markdown 表格輔助函數 ────────────────────────────────────────

def _ma_trend_tag(ema5, ema10, ema20, ema50) -> str:
    """均線排列狀態編碼：BULL_1 完美多頭，BULL_2 標準多頭，MIXED 混合，BEAR 空頭。"""
    vals = [ema5, ema10, ema20, ema50]
    if any(v is None or (isinstance(v, float) and v != v) for v in vals):
        return "N/A"
    if ema5 > ema10 > ema20 > ema50:
        return "BULL_1"
    if ema5 > ema20 > ema50:
        return "BULL_2"
    if ema20 < ema50:
        return "BEAR"
    return "MIXED"


def _macd_hist_tag(close: pd.Series) -> str:
    """MACD 直方圖狀態編碼：POS_INC / POS_DEC / NEG_INC / NEG_DEC / N/A。"""
    if len(close) < 36:
        return "N/A"
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    hist = (macd - signal).dropna()
    if len(hist) < 2:
        return "N/A"
    cur, prev = float(hist.iloc[-1]), float(hist.iloc[-2])
    if cur >= 0:
        return "POS_INC" if cur > prev else "POS_DEC"
    return "NEG_INC" if cur > prev else "NEG_DEC"


def _strategy_tag(indic: dict) -> str:
    """根據技術指標推薦最可能適用的策略標籤（MOMENTUM / BREAKOUT / REVERSAL / NEUTRAL）。"""
    rsi      = indic.get("rsi") or 0.0
    vol      = indic.get("volume_ratio") or 0.0
    stoch    = indic.get("stoch_k") or 50.0
    dist_20d = indic.get("dist_from_20d_high_pct") or -99.0
    rsi_prev = indic.get("rsi_5d_ago")
    ema5, ema20, ema50 = indic.get("ema5"), indic.get("ema20"), indic.get("ema50")

    if stoch < 25 and rsi_prev is not None and rsi > rsi_prev:
        return "REVERSAL"
    if vol >= 2.0 and -2.0 <= dist_20d <= 2.0:
        return "BREAKOUT"
    if ema5 and ema20 and ema50 and ema5 > ema20 > ema50 and 50 <= rsi <= 75 and vol >= 1.5:
        return "MOMENTUM"
    return "NEUTRAL"


def _calc_rs_vs_sector(
    sym: str,
    df: pd.DataFrame,
    sector: str,
    price_data: dict,
    sector_map: dict[str, str],
) -> float | None:
    """
    個股 5 日報酬率 − 同產業 equal-weight 籃子 5 日報酬率（複用 scorer.py 既有計算，
    取代美股版的 sector ETF 對照——台股無對應的完整 sector ETF 體系）。
    籃子樣本不足或 ^TWII 亦缺資料 → None。
    """
    stock_ret = _stock_5d_return(df)
    if stock_ret is None:
        return None

    benchmark_ret = _industry_basket_return(sym, sector, price_data, sector_map)
    if benchmark_ret is None:
        twii_df = price_data.get(market.BENCHMARK_TICKER)
        if twii_df is None or twii_df.empty:
            return None
        benchmark_ret = _stock_5d_return(twii_df)
        if benchmark_ret is None:
            return None

    return round(stock_ret - benchmark_ret, 1)


def _generate_candidates_markdown_table(
    candidates: list[dict],
    price_data: dict[str, pd.DataFrame],
    info_data: dict[str, dict],
    sector_map: dict[str, str] | None = None,
) -> str:
    """
    將 L2 候選股清單轉換為 Markdown 表格（含 RS_vs_Sector、基本面維度、
    Short_Float_Pct、ATR14）。
    """
    sector_map = sector_map or {}

    # ^TWII 報酬率序列在迴圈外預計算一次（Beta_60D 用）
    r_twii_global: pd.Series | None = None
    twii_df = price_data.get(market.BENCHMARK_TICKER)
    if twii_df is not None and not twii_df.empty:
        r_twii_global = twii_df["Close"].pct_change().dropna()

    header = (
        "| Ticker | Close_Price | Sector | L2_Score | Strategy_Tag | MA_Trend"
        " | EMA5 | EMA10 | EMA20 | Vol_vs_5DAvg | High_20D | Vol_vs_20DAvg"
        " | RSI | MACD_Hist | VTF_Score | Price_5D_Pct | Momentum_ATR | ATR14"
        " | EMA50 | Low_20D | Stoch_K | RSI_5D_Ago"
        " | RS_vs_Sector | Beta_60D"
        " | Fwd_PE | Profit_Margin | Rev_Growth_YoY | Short_Float_Pct |"
    )
    sep = (
        "|--------|-------------|--------|----------|--------------|----------"
        "|------|-------|-------|--------------|----------|--------------"
        "|-----|-----------|-----------|--------------|-------------|-------|"
        "-------|---------|---------|------------"
        "|--------------|----------"
        "|--------|----------------|-----------------|-----------------|"
    )
    rows = [header, sep]

    for c in candidates[:MAX_CANDIDATES_TO_AI]:
        sym = c["symbol"]
        df  = price_data.get(sym)
        if df is None:
            continue

        indic = compute_indicators(sym, df, r_twii_global=r_twii_global)
        info  = info_data.get(sym, {})
        close = df["Close"].dropna()

        ma_trend  = _ma_trend_tag(indic.get("ema5"), indic.get("ema10"), indic.get("ema20"), indic.get("ema50"))
        macd_tag  = _macd_hist_tag(close)
        strategy  = _strategy_tag(indic)
        price_str = f"NT${indic['price']:.2f}"

        ema5_v, ema10_v, ema20_v = indic.get("ema5"), indic.get("ema10"), indic.get("ema20")
        ema5_str  = f"NT${ema5_v:.2f}"  if ema5_v  is not None else "N/A"
        ema10_str = f"NT${ema10_v:.2f}" if ema10_v is not None else "N/A"
        ema20_str = f"NT${ema20_v:.2f}" if ema20_v is not None else "N/A"
        vol5_v = indic.get("vol_vs_5d_avg")
        vol5_str = f"{vol5_v:.2f}" if vol5_v is not None else "N/A"

        high20_v = indic.get("high_20d")
        high20_str = f"NT${high20_v:.2f}" if high20_v is not None else "N/A"
        vol20_v = indic.get("vol_vs_20d_avg")
        vol20_str = f"{vol20_v:.2f}" if vol20_v is not None else "N/A"

        rsi_val = indic.get("rsi")
        rsi_str = f"{rsi_val:.1f}" if rsi_val is not None else "N/A"

        vtf = indic.get("vtf_score")
        vtf_str = f"{vtf:.2f}" if vtf is not None else "N/A"

        p5d_str = f"{indic.get('change_5d_pct', 0.0):+.1f}%"

        mom = indic.get("momentum_atr")
        mom_str = f"{mom:.2f}" if mom is not None else "N/A"

        atr14_v = indic.get("atr14")
        atr14_str = f"NT${atr14_v:.2f}" if atr14_v is not None else "N/A"

        ema50_v = indic.get("ema50")
        ema50_str = f"NT${ema50_v:.2f}" if ema50_v is not None else "N/A"
        low20_v = indic.get("low_20d")
        low20_str = f"NT${low20_v:.2f}" if low20_v is not None else "N/A"
        stoch_v = indic.get("stoch_k")
        stoch_str = f"{stoch_v:.1f}" if stoch_v is not None else "N/A"
        rsi5ago_v = indic.get("rsi_5d_ago")
        rsi5ago_str = f"{rsi5ago_v:.1f}" if rsi5ago_v is not None else "N/A"

        sector_raw = c.get("sector") or info.get("sector", "")
        rs = _calc_rs_vs_sector(sym, df, sector_raw, price_data, sector_map)
        rs_str = f"{rs:+.1f}%" if rs is not None else "N/A"

        beta = indic.get("beta_60d")
        beta_str = f"{beta:.2f}" if beta is not None else "N/A"

        fwd_pe_v = info.get("forward_pe")
        fwd_pe_str = f"{fwd_pe_v:.1f}" if fwd_pe_v is not None else "N/A"
        margin_v = info.get("profit_margin")
        margin_str = f"{margin_v * 100:+.1f}%" if margin_v is not None else "N/A"
        rev_growth_v = info.get("revenue_growth")
        rev_growth_str = f"{rev_growth_v * 100:+.1f}%" if rev_growth_v is not None else "N/A"

        short_float_v = info.get("short_percent_float")
        short_float_str = f"{short_float_v * 100:.1f}%" if short_float_v is not None else "N/A"

        sector_display = sector_raw or "Unknown"

        rows.append(
            f"| {sym} | {price_str} | {sector_display} | {c['total_score']:.0f} | {strategy}"
            f" | {ma_trend} | {ema5_str} | {ema10_str} | {ema20_str} | {vol5_str} | {high20_str} | {vol20_str}"
            f" | {rsi_str} | {macd_tag} | {vtf_str}"
            f" | {p5d_str} | {mom_str} | {atr14_str} | {ema50_str} | {low20_str} | {stoch_str} | {rsi5ago_str}"
            f" | {rs_str} | {beta_str}"
            f" | {fwd_pe_str} | {margin_str} | {rev_growth_str} | {short_float_str} |"
        )

    return "\n".join(rows)


# ── 產業多樣性保護 ───────────────────────────────────────────────

def _diversify_candidates(
    candidates: list[dict],
    regime: str = "",
    max_per_sector: int = MAX_SECTOR_CANDIDATES,
) -> list[dict]:
    """
    對候選池做每產業上限截斷，防止同一板塊霸榜。
    PANIC_REVERSAL 環境下，total_score < 40 的強制放行反轉股不受產業配額限制。
    """
    sector_counts: dict[str, int] = {}
    result: list[dict] = []
    for c in sorted(candidates, key=lambda x: x["total_score"], reverse=True):
        sector = c.get("sector", "Unknown") or "Unknown"
        is_panic_force = (regime == "PANIC_REVERSAL" and c["total_score"] < 40.0)
        if is_panic_force or sector_counts.get(sector, 0) < max_per_sector:
            result.append(c)
            if not is_panic_force:
                sector_counts[sector] = sector_counts.get(sector, 0) + 1
    return result


# ── Prompt 建構 ──────────────────────────────────────────────────

def _build_prompt(
    candidates: list[dict],
    price_data: dict[str, pd.DataFrame],
    info_data: dict[str, dict],
    market_context: dict | None = None,
    sector_map: dict[str, str] | None = None,
    ai_hints: list[str] | None = None,
) -> str:
    """以 XML 標籤包裹三大區塊組裝結構化 Prompt；ai_hints 非空時在末尾附加
    <Historical_Performance_Review> 第四區塊。"""
    mc = market_context or {}
    regime_code = mc.get("regime", "")

    # ── <Market_Regime> ───────────────────────────────────────────
    regime_hint = mc.get("ai_prompt_hint", "")
    primary     = mc.get("primary_strategy", "")
    breadth     = mc.get("market_breadth_pct")
    vix_info    = mc.get("vix", {})
    index_info  = mc.get("index", {})
    sectors     = mc.get("sectors", {})

    regime_lines = []
    if regime_hint:
        regime_lines.append(regime_hint)

    extra_hint = _REGIME_EXTRA_HINT.get(regime_code, "")
    if extra_hint:
        regime_lines.append(extra_hint)

    stats = []
    if breadth is not None:
        stats.append(f"市場廣度={breadth:.1f}%")
    vix_val = vix_info.get("value")
    if vix_val is not None:
        stats.append(f"波動率={vix_val:.1f}%({vix_info.get('label', '')})")
    above_ema20 = index_info.get("above_ema20")
    if above_ema20 is not None:
        stats.append(f"加權指數={'EMA20之上' if above_ema20 else 'EMA20之下'}")
    if stats:
        regime_lines.append("大盤數據：" + "｜".join(stats))

    if sectors:
        sector_parts = [
            f"{sec} 5日={d.get('change_5d_pct', 0):+.1f}%"
            + (f" 20日={d.get('change_20d_pct'):+.1f}%" if d.get("change_20d_pct") is not None else "")
            for sec, d in sectors.items()
        ]
        regime_lines.append("產業籃子（同產業個股 equal-weight 平均漲跌）：" + "  ".join(sector_parts))

    regime_block = "\n".join(regime_lines)

    # ── <Candidate_Pool> ──────────────────────────────────────────
    table = _generate_candidates_markdown_table(candidates, price_data, info_data, sector_map=sector_map)
    field_defs = (
        "欄位定義：\n"
        "- MA_Trend: BULL_1=EMA5>EMA10>EMA20>EMA50完美多頭｜BULL_2=EMA5>EMA20>EMA50標準多頭｜MIXED=混合｜BEAR=空頭\n"
        "- EMA5/EMA10/EMA20: 指數移動均線價位（新台幣）。EMA10為動能策略淺回檔帶的下緣底線；"
        "股價已自然回落至EMA20~EMA10之間為結構確認最完整的加分情境\n"
        "- Vol_vs_5DAvg: 當日成交量÷5日均量；<0.7代表回檔量縮（拋壓衰竭，無恐慌賣壓），"
        "為動能策略回檔進場的量能確認條件\n"
        "- ATR14: 14日平均真實波幅（新台幣），衡量個股日常波動單位；"
        "動能策略的買入區間深度與止損距離以此為基準\n"
        "- High_20D: 20日最高價（新台幣），突破策略的壓力位/回測支撐基準\n"
        "- Vol_vs_20DAvg: 當日成交量÷20日均量；>=1.5代表攻擊量確認突破，<1.2假突破機率高\n"
        "- EMA50: 50日均線價位（新台幣），反轉策略的支撐區判斷基準\n"
        "- Low_20D: 20日最低價（新台幣），反轉策略的左側關鍵支撐與止損基準\n"
        "- Stoch_K: 隨機指標KD的K值；<25代表超賣區\n"
        "- RSI_5D_Ago: 5日前的RSI數值；RSI > RSI_5D_Ago代表價格雖弱但RSI回升，底背離訊號\n"
        "- MACD_Hist: POS_INC=正且遞增(最強)｜POS_DEC=正但遞減｜NEG_INC=負但回升｜NEG_DEC=負且下降(最弱)\n"
        "- VTF_Score: 量能推進因子=量比×(2×K線位置-1)，正值=帶量推進，負值=高檔出貨；>5.0=史詩級機構建倉\n"
        "- Price_5D_Pct: 近5日漲跌幅（短線爆發力）\n"
        "- Momentum_ATR: 20日價格位移÷ATR14（跨產業標準化動能）；>=2.0強勢；<=-2.0超賣\n"
        "- RS_vs_Sector: 個股5日報酬率-同產業籃子5日報酬率；>+2%=產業內領頭羊優先加分；<-2%=產業內落後者需額外確認；N/A=籃子樣本不足（同產業<3支候選）\n"
        "- Beta_60D: 60日Beta vs 加權指數(^TWII)；N/A=數據不足，不排除，改用Momentum_ATR判斷\n"
        "- Strategy_Tag: 系統預判策略（MOMENTUM/BREAKOUT/REVERSAL/NEUTRAL），僅供參考\n"
        "- Fwd_PE: 預估本益比（缺值時退化為trailing PE）；數值愈高代表市場對獲利的預期愈貴，"
        "同一批候選股橫向比較即可判斷相對貴賤；N/A=數據缺失，不代表不能選\n"
        "- Profit_Margin: 淨利率；正值愈高代表獲利體質愈紮實，負值代表虧損中；N/A=數據缺失，不代表不能選\n"
        "- Rev_Growth_YoY: 營收年增率；正值代表營收成長，負值代表營收衰退；N/A=數據缺失，不代表不能選\n"
        "- Short_Float_Pct: 空頭持股佔流通股比例；>15%視為高軋空風險（波動可能被空頭回補放大），"
        "應在risk中提示；N/A=數據缺失，不代表不能選"
    )
    pool_block = f"{table}\n\n{field_defs}"

    # ── <Output_Constraint> ───────────────────────────────────────
    strategy_line = (
        f"本日主推策略【{primary}】，只選符合此策略邏輯的個股。"
        if primary else "全面防禦模式，不建議建立新倉位。"
    )
    constraint_block = (
        f"必須無條件服從 <Market_Regime> 的策略方向。\n"
        f"{strategy_line}\n"
        f"從 <Candidate_Pool> 中篩選最多 3 支最佳標的。\n"
        f"【型態限制】hold_period 必須輸出純整數（Integer），代表預期持有的台股交易日天數，"
        f"嚴禁輸出任何非數字字元（禁止如 '5 天'、'2 週'、'5~7' 等字串格式）。\n"
        f'以 JSON 格式輸出，不附加任何說明文字：{{"selections": [{{...}}, ...]}}'
    )

    prompt = (
        f"<Market_Regime>\n{regime_block}\n</Market_Regime>\n\n"
        f"<Candidate_Pool>\n{pool_block}\n</Candidate_Pool>\n\n"
        f"<Output_Constraint>\n{constraint_block}\n</Output_Constraint>"
    )

    if ai_hints:
        review_block = (
            "以下為本系統過往已結算訊號的實戰統計回饋：\n"
            + "\n".join(ai_hints)
            + "\n樣本數有限，僅供權衡參考，不得覆蓋 Market_Regime 的策略方向。"
        )
        prompt += (
            f"\n\n<Historical_Performance_Review>\n{review_block}\n</Historical_Performance_Review>"
        )

    return prompt


SYSTEM_PROMPT = """你是一位經驗豐富的台股量化分析師，擅長技術面與動能選股。
候選池（<Candidate_Pool>）已經過系統流動性與技術強度雙重篩選，技術面同質性偏高，
你的任務是從中根據技術指標、量價關係、趨勢動能，並疊加基本面（估值、獲利品質、成長性）作最終取捨，
挑選出你認為值得「買入」的標的（最多 3 支），並給出具體操作建議。
若符合買入條件的標的不足 3 支，只輸出實際符合條件的數量，不要勉強湊數。

選股原則：
1. 優先選擇均線多頭排列完整、RSI 健康（50~70）、MACD 向上的個股
2. VTF_Score > 1.0 代表帶量推進（主力進場），VTF_Score < 0 代表高檔出貨，後者應大幅降權
3. RS_vs_Sector > +2%：產業內領頭羊，同等條件下優先選擇；RS_vs_Sector < -2%：產業內落後者，需額外確認
4. Momentum_ATR 為跨產業標準化動能（ATR 倍數），比絕對漲跌幅更公平；>=2.0 代表強勢動能
5. 避免過度集中於同一產業（已由系統做初步分散，但 AI 可進一步考量）
6. 技術面強度相近的候選股之間，優先選擇 Profit_Margin 為正、Rev_Growth_YoY 為正、Fwd_PE 相對同批候選股
   不過度偏貴的個股；若技術面強但基本面明顯空心（虧損、營收衰退、估值過高），不必直接排除，
   但應在 confidence 給予較低分數，並在 risk 中具體說明基本面疑慮

市場背景判斷原則：
- 大盤（加權指數 ^TWII）：若大盤 5 日跌幅 > 2% 或處於 EMA20 之下，整體提高警覺，傾向「觀望」
- 波動率：數值偏高時市場恐慌情緒高，操作建議應更保守；數值偏低代表市場樂觀，可積極
- 產業籃子：個股所屬產業籃子若近 5 日下跌，即使個股技術面佳也需提示風險；籃子強勢則加分
- 產業籃子的趨勢應反映在 reason 中，說明產業走勢對個股的支撐或壓制

【動能策略（momentum）】：
- 條件：均線多頭排列（EMA5>EMA10>EMA20>EMA50）、RSI 50~70、VTF_Score >= 1.0
- 優先：Momentum_ATR >= 2.0 且 RS_vs_Sector > +1%（產業內領頭羊動能延續）
- 買入區間（以個股自身波動 ATR14 為深度基準，等待淺回檔而非深度回檔）：
  1. 標準進場（預設）：buy_zone 設在 Close_Price − 1×ATR14 ～ Close_Price − 0.25×ATR14 的淺回檔帶
     （強勢股的正常日內波動就會觸及，不需要跌破趨勢才能進場）；
     若該區間下緣低於 EMA10，改以 EMA10 為下緣（不追求超過趨勢結構的深度回檔）
  2. 已回檔加分情境：股價已自然回落至 EMA20~EMA10 之間，且 Vol_vs_5DAvg < 0.7（量縮無賣壓）
     → buy_zone 直接設在該 EMA20~EMA10 區間（結構確認最完整，信心分數可上調）
  3. 過熱例外：RSI > 78 或 VTF_Score < 0（高檔出貨跡象）→ 大幅降低信心分數，不宜進場，寧可錯過
- 止損：買入區間下緣 − 1×ATR14（以波動單位計，非固定百分比；不得等於買入區間下緣）；
  若股價回落至 EMA20~EMA10 帶內進場（第 2 情境），止損可改用 EMA20 下方 2%（兩者取較高者，
  止損不得寬於進場價 −10%）
- 目標：+10%~20%；持有：1~4 週

【突破策略（breakout）】：
- 條件：VTF_Score >= 1.5、Close_Price 距 High_20D 在 -2%~+2% 內
- 買入區間（拒絕盲目追單，優先選有回測確認的進場點）：
  1. 回測確認（優先，勝率最高）：股價曾站上 High_20D 後回落至 High_20D~High_20D×1.02 區間企穩（壓力轉支撐）→ buy_zone 設在此區間
  2. 標準突破緩衝：Close_Price 站上 High_20D 且距 High_20D 在 +0.5%~+1.5% 內 → buy_zone 設在此緩衝帶
  3. 距 High_20D 超過 +3%（追高）→ 大幅降低信心分數，不宜以此時收盤價設定買入區間
  4. 量能確認：Vol_vs_20DAvg >= 1.5 才視為攻擊量確認；< 1.2 假突破機率高，降低信心分數
- VTF_Score < 0 一律視為假突破派發陷阱，禁止入選
- 目標：+10%~20%；止損：跌回 High_20D 下方 2%
- 持有：1~2 週

【反轉策略（oversold_reversal）】：
- 條件：Momentum_ATR <= -2.0（超賣）且 VTF_Score 由負轉正或向 0 軸收斂（拋壓衰竭）
- 底背離確認：Stoch_K < 25 且 RSI > RSI_5D_Ago（價格弱勢但 RSI 未同步破底，拋壓衰竭訊號）
- 買入區間（拒絕盲目接刀，優先選右側結構確認的進場點）：
  1. Close_Price 落在 EMA50 附近（±3% 內）且底背離條件成立 → buy_zone 設在 EMA50 附近（EMA50×0.98~EMA50×1.02）
  2. Close_Price 須已明顯高於 Low_20D（代表右側反彈已確立，非左側接刀），buy_zone 下緣不得低於 Low_20D
  3. 若 Close_Price 仍貼近或跌破 Low_20D（尚未右側確認），不宜進場，維持觀望
- PANIC_REVERSAL 環境下，帶有 REVERSAL 標籤的個股 L2_Score 偏低是正常的，忽略低分偏見
- 目標：+8%~15%；止損：Low_20D 下方 2%（不得設在 EMA50 之上，避免止損過寬；不得等於買入區間下緣）

N/A 差異化處理：
- Momentum_ATR = N/A 或 VTF_Score = N/A → 直接排除（技術數據不足）
- Beta_60D = N/A → 不排除，以 Momentum_ATR 和 VTF_Score 作為核心多空判斷
- RS_vs_Sector = N/A → 不排除，僅代表同產業候選股樣本不足，改倚重 Momentum_ATR/VTF_Score 判斷
- Fwd_PE、Profit_Margin、Rev_Growth_YoY 任一為 N/A → 不排除，僅代表該基本面維度無法評估，改倚重其餘維度判斷
- Short_Float_Pct = N/A → 不排除，僅代表放空數據缺失

請以如下 JSON 格式輸出（根節點為物件，陣列放在 "selections" key 中），不要其他說明文字：
{"selections": [ {...}, {...}, ... ]}

每個元素包含：
- rank: 排名（整數，從 1 開始）
- ticker: 股票代號
- reason: 繁體中文選股理由（80~120字）。聚焦技術指標數值「以外」的論述：基本面體質（Fwd_PE/Profit_Margin/Rev_Growth_YoY
  相對同批候選股的優劣及其意涵）、所屬產業籃子趨勢對個股的支撐或壓制、與當日 Market_Regime 主推策略的契合度、
  Short_Float_Pct 等特殊風險背景。技術指標數值（RSI/VTF/EMA/Momentum_ATR 等）一律放在 strategy_reason，
  reason 中禁止重複羅列指標數字，技術面最多以一句定性描述帶過（例如「量價結構健康」）
- risk: 繁體中文風險提示（50字以內）
- confidence: 信心分數（整數 1~10）
- buy_zone: 建議買入價格區間，格式如 "NT$185～NT$188"
- target: 目標價，格式如 "NT$210"
- stop_loss: 止損價，格式如 "NT$180"
- hold_period: 預期持有的台股交易日天數，純整數（例如 10），不得含任何非數字字元
- strategy: 套用的選股策略，只能是「動能策略」、「突破策略」、「反轉策略」三者之一
- strategy_reason: 繁體中文，說明選擇此策略的具體依據，需引用指標數值（例如：RSI=62、VTF=2.3、RS=+3.1%）（50字以內）
- confidence_reason: 繁體中文，說明信心分數給分原因，需具體說明加分或扣分的主因（50字以內）"""


# ── DeepSeek API 呼叫 ────────────────────────────────────────────

def _call_deepseek(user_content: str) -> list[dict]:
    """呼叫 DeepSeek API，回傳解析後的排名列表。"""
    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                response_format={"type": "json_object"},
                temperature=0.3,
                max_tokens=6000,
            )
            raw = resp.choices[0].message.content.strip()
            finish_reason = resp.choices[0].finish_reason
            print(f"[ranker] API 回傳 {len(raw)} 字元，finish_reason={finish_reason}")
            if finish_reason == "length":
                print("[ranker] 警告：回應因 max_tokens 截斷，考慮再調高 max_tokens")

            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
            for v in parsed.values():
                if isinstance(v, list):
                    print(f"[ranker] 解析成功，取得 {len(v)} 筆結果")
                    return v
            return []

        except json.JSONDecodeError as e:
            print(f"[ranker] JSON 解析失敗（第{attempt}次）：{e}")
            match = re.search(r"\[.*\]", raw, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except Exception:
                    pass
        except Exception as e:
            print(f"[ranker] API 呼叫失敗（第{attempt}次）：{e}")

        if attempt < MAX_RETRIES:
            time.sleep(5 * attempt)

    return []


# ── 主函式 ───────────────────────────────────────────────────────

def _enrich_fallback(
    candidates: list[dict],
    info_data: dict[str, dict],
    price_data: dict[str, pd.DataFrame],
) -> list[dict]:
    """為 fallback（不呼叫 AI）的候選股補充 name/sector/price_data 欄位。"""
    result = []
    for i, c in enumerate(candidates):
        sym = c["symbol"]
        info = info_data.get(sym, {})
        result.append({
            **c,
            "rank": i + 1,
            "name": info.get("name", sym),
            "sector": c.get("sector") or info.get("sector", "Unknown"),
            "reason": "L2 技術指標評分排名",
            "risk": "請手動確認各項指標",
            "confidence": 5,
            "is_fallback": True,  # 非真實 AI 判斷，僅 L2 分數排序頂替
            "buy_zone": "-",
            "target": "-",
            "stop_loss": "-",
            "hold_period": "-",
            "strategy": "-",
            "strategy_reason": "",
            "confidence_reason": "",
            "_price_data": price_data.get(sym),
        })
    return result


def rank_candidates(
    candidates: list[dict],
    price_data: dict[str, pd.DataFrame],
    info_data: dict[str, dict],
    top_n: int = 3,
    market_context: dict | None = None,
    market_date: str | None = None,
    use_ai_cache: bool = True,
    sector_map: dict[str, str] | None = None,
) -> list[dict]:
    """
    接收 L2 候選股，呼叫 DeepSeek AI 排序，回傳 Top N 結果。
    每個結果含原始 L2 資料 + AI 排名/理由/信心分數。
    同日重複執行時，若 use_ai_cache=True 則複用 .cache/ranked_YYYYMMDD.json，
    不重複呼叫 DeepSeek API。
    """
    if not candidates:
        print("[ranker] 無候選股，跳過 AI 排序")
        return []

    if not DEEPSEEK_API_KEY:
        print("[ranker] 未設定 DEEPSEEK_API_KEY，跳過 AI 排序，改用 L2 分數直接輸出 Top N")
        return _enrich_fallback(candidates[:top_n], info_data, price_data)

    market_context = market_context or {}
    regime = market_context.get("regime", "")

    # BEAR_DISTRIBUTION 防禦機制：直接回傳空列表，不送 AI 請求
    if regime == "BEAR_DISTRIBUTION":
        print("[ranker] 大盤進入【陰跌熊市 BEAR_DISTRIBUTION】，系統全面防禦，不輸出買入標的")
        return []

    # 產業多樣性保護：送給 AI 前先截斷同產業霸榜
    diversified = _diversify_candidates(candidates, regime=regime)
    print(f"[ranker] 候選池：{len(candidates)} → 產業分散後 {len(diversified)} 支")

    cache_path = _ranked_cache_path(market_date or date.today().isoformat())

    # 嘗試讀取 AI 快取（同日重複執行時避免重複呼叫 API）
    if use_ai_cache and cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            for item in cached:
                item["_price_data"] = price_data.get(item["symbol"])
            print(f"[ranker] 複用今日 AI 快取（{cache_path.name}），共 {len(cached)} 支")
            return cached[:top_n]
        except Exception as e:
            print(f"[ranker] AI 快取讀取失敗，重新呼叫 DeepSeek：{e}")

    ai_hints = _load_ai_hints()
    if ai_hints:
        print(f"[ranker] 附加 {len(ai_hints)} 條歷史績效回饋至 Prompt（ai_hints.json）")

    print(f"[ranker] 送出 {min(len(diversified), MAX_CANDIDATES_TO_AI)} 支候選股給 DeepSeek AI...")
    prompt_content = _build_prompt(
        diversified, price_data, info_data, market_context,
        sector_map=sector_map, ai_hints=ai_hints,
    )

    ranked_raw = _call_deepseek(prompt_content)
    if not ranked_raw:
        print("[ranker] AI 排序失敗，改用 L2 分數直接輸出 Top N")
        return _enrich_fallback(candidates[:top_n], info_data, price_data)

    # 建立 L2 資料查詢表（原始 candidates，含全部通過 L2 的個股）
    l2_map = {c["symbol"]: c for c in candidates}

    ranked: list[dict] = []
    for item in ranked_raw:
        ticker = str(item.get("ticker", "")).strip().upper()
        l2 = l2_map.get(ticker, {})
        ranked.append({
            "rank": int(item.get("rank", len(ranked) + 1)),
            "symbol": ticker,
            "name": info_data.get(ticker, {}).get("name", ticker),
            "sector": l2.get("sector") or info_data.get(ticker, {}).get("sector", "Unknown"),
            "price": l2.get("price", 0.0),
            "total_score": l2.get("total_score", 0.0),
            "reason": str(item.get("reason", "")),
            "risk": str(item.get("risk", "")),
            "confidence": int(item.get("confidence", 5)),
            "buy_zone": str(item.get("buy_zone", "-")),
            "target": str(item.get("target", "-")),
            "stop_loss": str(item.get("stop_loss", "-")),
            "hold_period": str(item.get("hold_period", "-")),
            "strategy": str(item.get("strategy", "-")),
            "strategy_reason": str(item.get("strategy_reason", "")),
            "confidence_reason": str(item.get("confidence_reason", "")),
            "_price_data": price_data.get(ticker),
        })

    ranked.sort(key=lambda x: x["rank"])
    result = ranked[:top_n]
    print(f"[ranker] AI 排序完成，回傳 Top {len(result)}")

    # 儲存 AI 結果至快取（_price_data 為 DataFrame，不序列化）
    try:
        _CACHE_DIR.mkdir(exist_ok=True)
        serializable = [{k: v for k, v in item.items() if k != "_price_data"} for item in result]
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
        print(f"[ranker] AI 結果已快取：{cache_path.name}")
    except Exception as e:
        print(f"[ranker] AI 快取儲存失敗（不影響結果）：{e}")

    return result

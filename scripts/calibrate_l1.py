#!/usr/bin/env python3
"""
一次性校準腳本：用當前 .cache 的價量與基本面數據，分析 L1 三項門檻
（30日均成交金額、市值、ATR%）的實際分布，用於收斂 filter.py 的暫定門檻。

前提：已跑過 python main.py --dry-run（或 --no-cache），.cache/ 內有當日
price_*.pkl 與 info_*.json，且 data/universe_roster.json 已寫入最終名單。

Usage:
  python scripts/calibrate_l1.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import numpy as np

import fetcher
from filter import _atr_pct

ROSTER_PATH = Path(__file__).parent.parent / "data" / "universe_roster.json"


def main() -> None:
    with open(ROSTER_PATH, "r", encoding="utf-8") as f:
        roster = json.load(f)
    symbols = roster["symbols"]
    print(f"[calibrate_l1] 名單：{len(symbols)} 支（{roster['market_date']}）")

    price_data = fetcher.load_price_cache()
    info_data = fetcher.load_info_cache()
    if price_data is None or info_data is None:
        print("[calibrate_l1] 錯誤：找不到今日快取，請先跑 python main.py --dry-run")
        return

    trade_values, market_caps, atr_pcts = [], [], []
    for sym in symbols:
        df = price_data.get(sym)
        if df is None or df.empty:
            continue
        close = df["Close"].dropna()
        volume = df["Volume"].dropna()
        if not close.empty and not volume.empty:
            n = min(30, len(close), len(volume))
            trade_values.append(float((close.tail(n) * volume.tail(n)).mean()))

        mc = (info_data.get(sym) or {}).get("market_cap")
        if mc:
            market_caps.append(float(mc))

        atr = _atr_pct(df)
        if atr is not None:
            atr_pcts.append(atr)

    percentiles = [5, 10, 20, 30, 50]

    print(f"\n[calibrate_l1] 30日均成交金額（NT$，樣本數={len(trade_values)}）：")
    for p in percentiles:
        print(f"  P{p:>2}: NT${np.percentile(trade_values, p)/1e6:.1f}M")

    print(f"\n[calibrate_l1] 市值（NT$，樣本數={len(market_caps)}）：")
    for p in percentiles:
        print(f"  P{p:>2}: NT${np.percentile(market_caps, p)/1e8:.1f}億")

    print(f"\n[calibrate_l1] ATR%（樣本數={len(atr_pcts)}）：")
    for p in [50, 70, 90, 95, 99]:
        print(f"  P{p:>2}: {np.percentile(atr_pcts, p):.2f}%")

    print("\n[calibrate_l1] 建議：門檻設在 P5~P10 附近（近似排除名單尾部 5~10% 的極端值，")
    print("  而非大幅收斂名單，因為 universe 本身已是流動性前 150~180 名）")
    print(f"  MIN_DAILY_TRADE_VALUE ~= NT${np.percentile(trade_values, 10)/1e6:.0f}M（P10）")
    print(f"  MIN_MARKET_CAP        ~= NT${np.percentile(market_caps, 10)/1e8:.0f}億（P10）")
    print(f"  MAX_ATR_PCT           ~= {np.percentile(atr_pcts, 95):.1f}%（P95，排除最極端的5%）")


if __name__ == "__main__":
    main()

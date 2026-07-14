#!/usr/bin/env python3
"""
一次性校準腳本：用 ^TWII 歷史 HV20（20日已實現波動率，年化）分布，
校準 market.py 的 HV_LOW_THRESHOLD / HV_HIGH_THRESHOLD。

校準方法：不是照抄美股 VIX 20/25 的絕對數值（HV 與 VIX 語意不同，見
market.py 模組說明），而是對齊「Regime 出現頻率」——美股版 VIX<20 大約
覆蓋七成交易日、VIX>=25 大約一成，這裡取 ^TWII HV20 的第 70 / 90 百分位
分別當 HV_LOW_THRESHOLD / HV_HIGH_THRESHOLD。

Usage:
  python scripts/calibrate_hv.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import numpy as np
import pandas as pd
import yfinance as yf

BENCHMARK_TICKER = "^TWII"
PERIOD = "5y"


def main() -> None:
    print(f"[calibrate_hv] 下載 {BENCHMARK_TICKER} {PERIOD} 歷史數據...")
    df = yf.download(BENCHMARK_TICKER, period=PERIOD, interval="1d", auto_adjust=True, progress=False)
    close_col = df["Close"] if "Close" in df.columns else pd.Series(dtype=float)
    close = (close_col.squeeze() if isinstance(close_col, pd.DataFrame) else close_col).dropna()
    print(f"[calibrate_hv] 取得 {len(close)} 個交易日（{close.index[0].date()} ~ {close.index[-1].date()}）")

    log_ret = np.log(close / close.shift(1)).dropna()
    hv20 = (log_ret.rolling(20).std() * np.sqrt(252) * 100).dropna()
    print(f"[calibrate_hv] HV20 樣本數：{len(hv20)}")

    percentiles = [10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 99]
    print("\n[calibrate_hv] HV20 分位數表（年化 %）：")
    for p in percentiles:
        print(f"  P{p:>2}: {np.percentile(hv20, p):.2f}%")

    hv_low = float(np.percentile(hv20, 70))
    hv_high = float(np.percentile(hv20, 90))
    print(f"\n[calibrate_hv] 建議 HV_LOW_THRESHOLD（P70）  = {hv_low:.2f}")
    print(f"[calibrate_hv] 建議 HV_HIGH_THRESHOLD（P90） = {hv_high:.2f}")
    print(f"\n[calibrate_hv] 資料窗：{close.index[0].date()} ~ {close.index[-1].date()}（{PERIOD}）")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
一次性校準腳本：掃描 universe 近 3 年日線數據，分別找出滿足跌停鎖死 D1+D2
（`tracker.is_limit_down_locked` 的「收盤=最低」+「比值 sanity」兩條件）與
滿足一字漲停 U1+U2（`tracker.is_one_price_limit_up` 的「全日單一價位」+
「方向向上」兩條件）的候選 bar，檢視兩側各自的量能比（volume / vol_ma20）
分布，用於校準 `tracker.LOCK_VOLUME_RATIO`（D3/U3 的量能枯竭門檻，見
docs/phase3_limit_lock_design.md §3.1 R2）。

不假設兩側同分布，分別報告分位數，供人工判讀「預期鎖死 vs 盤中打開/爆量倒貨」
的雙峰分界（§3.1：「兩側可各自定值，不假設同分布」）。本腳本只印出分布報告，
不自動寫回 tracker.py——門檻異動需人工確認並同步更新 tests/test_tracker.py
的 fixture 數值。

vol_ma20 定義與 `tracker._fetch_latest` 完全一致（不含當日的前 20 個交易日
均量，不足 10 根有效值視為樣本不足並排除該筆），確保校準結果與正式判定同標尺。

Usage:
  python scripts/calibrate_lock.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import numpy as np
import pandas as pd
import yfinance as yf

PERIOD = "3y"
BATCH_SIZE = 50
_ROSTER_PATH = Path(__file__).parent.parent / "data" / "universe_roster.json"


def _vol_ma20_series(volume: pd.Series) -> pd.Series:
    """不含當日的前 20 個交易日均量，同 tracker._fetch_latest 的定義；
    樣本不足 10 根 → NaN（校準時直接排除，不像正式判定那樣保守視為成立，
    因為校準腳本的目的是觀察真實量能比分布，樣本不足的雜訊點沒有校準價值）。"""
    return volume.shift(1).rolling(20, min_periods=10).mean()


def _scan_symbol(df: pd.DataFrame) -> tuple[list[float], list[float]]:
    """回傳 (down_ratios, up_ratios)：分別是滿足 D1+D2 / U1+U2 候選 bar 的量能比。"""
    close = df["Close"].dropna()
    if len(close) < 30:
        return [], []

    aligned = df.loc[close.index]
    high   = aligned["High"]   if "High"   in aligned.columns else pd.Series(index=close.index, dtype=float)
    low    = aligned["Low"]    if "Low"    in aligned.columns else pd.Series(index=close.index, dtype=float)
    volume = aligned["Volume"] if "Volume" in aligned.columns else pd.Series(index=close.index, dtype=float)

    prev_close = close.shift(1)
    vol_ma20 = _vol_ma20_series(volume)

    down_ratios: list[float] = []
    up_ratios: list[float] = []

    for i in range(1, len(close)):
        pc, c = prev_close.iloc[i], close.iloc[i]
        l, h, v, vma = low.iloc[i], high.iloc[i], volume.iloc[i], vol_ma20.iloc[i]

        if pd.isna(pc) or pc <= 0 or pd.isna(c) or pd.isna(l) or pd.isna(v) or pd.isna(vma) or vma <= 0:
            continue

        ratio = float(v / vma)
        price_ratio = c / pc

        # D1+D2（跌停形，設計 §3.1；不含 D3，D3 正是本腳本要校準的對象）
        if 0.5 <= price_ratio <= 0.906 and (c - l) <= pc * 0.002:
            down_ratios.append(ratio)

        # U1+U2（漲停形，設計 §3.2；不含 U3）
        if not pd.isna(h) and (h - l) <= pc * 0.002 and c > pc:
            up_ratios.append(ratio)

    return down_ratios, up_ratios


def _report(label: str, ratios: list[float]) -> None:
    percentiles = [5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 95]
    if not ratios:
        print(f"\n[calibrate_lock] {label}候選 bar 樣本數為 0，無法校準（universe 可能無足夠漲跌停歷史）")
        return
    print(f"\n[calibrate_lock] {label}候選 bar 數：{len(ratios)}")
    print(f"[calibrate_lock] {label}量能比（volume / vol_ma20）分位數：")
    for p in percentiles:
        print(f"  P{p:>2}: {np.percentile(ratios, p):.3f}")


def main() -> None:
    if not _ROSTER_PATH.exists():
        print("[calibrate_lock] 錯誤：找不到 data/universe_roster.json，請先跑 python main.py --dry-run")
        return
    with open(_ROSTER_PATH, "r", encoding="utf-8") as f:
        roster = json.load(f)
    symbols = roster["symbols"]
    print(f"[calibrate_lock] 名單：{len(symbols)} 支（{roster['market_date']}），下載 {PERIOD} 歷史數據...")

    all_down: list[float] = []
    all_up: list[float] = []

    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i : i + BATCH_SIZE]
        print(f"[calibrate_lock] 下載 {i+1}~{min(i+BATCH_SIZE, len(symbols))} / {len(symbols)}")
        try:
            raw = yf.download(
                tickers=batch, period=PERIOD, interval="1d",
                group_by="ticker", auto_adjust=True, progress=False, threads=True,
            )
        except Exception as e:
            print(f"[calibrate_lock] 批次下載失敗，跳過：{e}")
            continue

        for sym in batch:
            try:
                df = raw[sym].copy() if isinstance(raw.columns, pd.MultiIndex) else raw.copy()
                df = df.dropna(how="all")
            except Exception:
                continue
            if df.empty:
                continue
            down, up = _scan_symbol(df)
            all_down.extend(down)
            all_up.extend(up)

    _report("跌停形（D1+D2）", all_down)
    _report("漲停形（U1+U2）", all_up)

    print("\n[calibrate_lock] 校準建議：")
    print("  預期分布為雙峰（鎖死 vs 盤中打開/爆量倒貨），LOCK_VOLUME_RATIO 應落在兩峰之間的谷底。")
    print("  請人工檢視上方分位數表判讀谷底位置；跌停/漲停兩側若谷底明顯不同，")
    print("  可比照設計 §3.1 拆成兩個獨立常數（目前 tracker.py 共用一個 LOCK_VOLUME_RATIO）。")
    print("  校準後記得同步更新 tests/test_tracker.py 中依賴 LOCK_VOLUME_RATIO=0.3 的 fixture 數值。")


if __name__ == "__main__":
    main()

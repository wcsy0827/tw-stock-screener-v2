#!/usr/bin/env python3
"""
一次性校準腳本：掃描 universe 近 3 年日線數據，校準 `tracker.LOCK_VOLUME_RATIO`
（D3/U3 的量能枯竭門檻，見 docs/phase3_limit_lock_design.md §3.1 R2 與附錄 B）。

**方法論（2026-07 實跑後修正）**：原方案「從 D1+D2 候選池找雙峰谷底」已被實測
證偽——該候選池混合「真鎖死無量」與「盤中重挫但正常放量」兩群，混池的量能比
分布是右偏長尾、無雙峰。正確定錨方法是**一字切分**：跌停形候選中「全日單一
價位」（high−low ≤ 0.002×prev_close）的一字 bar 是無可爭辯的真鎖死（全天
只在跌停價成交，任何賣單都不可能成交），以這群的量能比分位數定錨門檻；
盤中殺至跌停的非一字 bar 是污染群，僅供對照誤鎖率，不參與定錨。

判讀注意（見附錄 B 抗辯記錄）：
- 一字 bar 樣本可能高度集中於單一系統性崩盤事件（2026-07 實跑時 85% 集中在
  2025-04），須剔除集中事件檢視分位數穩健性，勿以單一事件尾端定錨。
- 一字群量能比高的尾端（>0.6）半數隔日即解鎖（主力跌停接貨），把門檻拉高去
  涵蓋它們會擴大誤鎖面卻只撿回淺偏差案例，不值得。
- 樣本取自今日在冊 roster，鎖死至下市的股票不在內（存活者偏差，已知限制）。

本腳本只印出分布報告，不自動寫回 tracker.py——門檻異動需人工確認並同步更新
tests/test_tracker.py 的 fixture 數值。

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


def _scan_symbol(df: pd.DataFrame) -> tuple[list[tuple[float, str, bool]], list[float]]:
    """回傳 (down_recs, up_ratios)。down_recs 每筆為 (量能比, 日期, 是否一字)：
    一字（high−low ≤ 0.002×prev_close）= 確定真鎖死，是定錨主體；非一字 =
    盤中殺至跌停的污染群，僅供對照。up_ratios 為 U1+U2 候選的量能比。"""
    close = df["Close"].dropna()
    if len(close) < 30:
        return [], []

    aligned = df.loc[close.index]
    high   = aligned["High"]   if "High"   in aligned.columns else pd.Series(index=close.index, dtype=float)
    low    = aligned["Low"]    if "Low"    in aligned.columns else pd.Series(index=close.index, dtype=float)
    volume = aligned["Volume"] if "Volume" in aligned.columns else pd.Series(index=close.index, dtype=float)

    prev_close = close.shift(1)
    vol_ma20 = _vol_ma20_series(volume)

    down_recs: list[tuple[float, str, bool]] = []
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
            one_price = (not pd.isna(h)) and (h - l) <= pc * 0.002
            down_recs.append((ratio, str(close.index[i].date()), one_price))

        # U1+U2（漲停形，設計 §3.2；不含 U3）
        if not pd.isna(h) and (h - l) <= pc * 0.002 and c > pc:
            up_ratios.append(ratio)

    return down_recs, up_ratios


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

    all_down: list[tuple[float, str, bool]] = []
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

    # 一字切分：一字 bar（真鎖死）是定錨主體，非一字（盤中殺跌）僅供對照
    one_price = [r for r, _, op in all_down if op]
    intraday  = [r for r, _, op in all_down if not op]
    _report("跌停形・一字（真鎖死，定錨主體）", one_price)
    _report("跌停形・非一字（盤中殺跌污染群，僅供對照）", intraday)
    _report("漲停形（U1+U2）", all_up)

    # 事件集中度：一字樣本若集中於單一崩盤事件，分位數會被該事件主導
    from collections import Counter
    months = Counter(date[:7] for r, date, op in all_down if op)
    top = months.most_common(3)
    total_one = len(one_price)
    if total_one:
        print(f"\n[calibrate_lock] 一字樣本月份集中度（top 3）：")
        for m, cnt in top:
            print(f"  {m}: {cnt} 筆（{cnt/total_one:.0%}）")
        if top and top[0][1] / total_one > 0.5:
            ex_event = [r for r, date, op in all_down if op and date[:7] != top[0][0]]
            _report(f"跌停形・一字・剔除 {top[0][0]}（穩健性檢查）", ex_event)

    print("\n[calibrate_lock] 校準建議（方法論詳見設計文件 §3.1 補述與附錄 B）：")
    print("  以「一字（真鎖死）」群的分位數定錨 LOCK_VOLUME_RATIO（P90~P95 帶），")
    print("  並用剔除集中事件後的分位數檢查穩健性——勿以單一崩盤事件的尾端定錨。")
    print("  一字群高量尾端多為主力跌停接貨、隔日即解鎖，涵蓋它們只會擴大誤鎖面。")
    print("  非一字群僅供評估誤鎖率（門檻下有多少盤中殺跌 bar 會被誤判鎖死）。")
    print("  校準後記得同步更新 tests/test_tracker.py 中依賴 LOCK_VOLUME_RATIO 的 fixture 數值。")


if __name__ == "__main__":
    main()

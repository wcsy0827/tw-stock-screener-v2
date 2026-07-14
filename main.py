#!/usr/bin/env python3
"""
台股選股系統 MVP — Phase 1（Universe → 抓資料 → 簡化 Regime → L2 技術評分）

只跑通資料管線與候選池分數分布驗證，不接 L3 AI 精選、不做 tracker 追蹤、不發布報告。

Usage:
  python main.py --dry-run
  python main.py --dry-run --no-cache
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from dotenv import load_dotenv

load_dotenv()

import fetcher
import filter as filter_
import market
import scorer
import universe


OUTPUT_PATH = Path(__file__).parent / "data" / "candidates.json"


def run(no_cache: bool = False) -> dict:
    print("[main] Step 1: Universe")
    symbols, sector_map = universe.fetch_universe()

    print("[main] Step 2: 抓取日 K 數據")
    fetch_symbols = symbols + [market.BENCHMARK_TICKER]

    price_data = None if no_cache else fetcher.load_price_cache()
    if price_data is None:
        price_data = fetcher.fetch_batch(fetch_symbols)
        fetcher.save_price_cache(price_data)

    price_data = fetcher.trim_incomplete_session(price_data)

    if market.BENCHMARK_TICKER not in price_data:
        print(f"[main] 警告：{market.BENCHMARK_TICKER} 下載失敗，Regime/RS fallback 將受影響")

    print("[main] Step 2.5: 簡化 Regime 判定")
    regime, breadth_pct, hv20, hv_ok = market.fetch_regime_quick(price_data)

    print("[main] Step 3: 抓取基本面資訊")
    info_data = None if no_cache else fetcher.load_info_cache()
    if info_data is None:
        info_data = fetcher.fetch_info(symbols)
        fetcher.save_info_cache(info_data)

    print("[main] Step 4: L1 流動性篩選")
    l1_passed = filter_.apply_filters(
        {s: price_data[s] for s in symbols if s in price_data},
        info_data,
    )

    print("[main] Step 5: L2 技術評分")
    candidates = scorer.score_all(l1_passed, price_data, regime=regime, sector_map=sector_map)

    market_date = str(price_data[market.BENCHMARK_TICKER].index[-1].date()) if market.BENCHMARK_TICKER in price_data else str(date.today())

    result = {
        "market_date": market_date,
        "regime": regime,
        "breadth_pct": breadth_pct,
        "hv20": hv20,
        "hv_ok": hv_ok,
        "universe_count": len(symbols),
        "l1_passed_count": len(l1_passed),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="台股選股系統 MVP")
    parser.add_argument("--dry-run", action="store_true", help="輸出候選池 JSON，不做其他動作")
    parser.add_argument("--no-cache", action="store_true", help="忽略快取，強制重新下載")
    args = parser.parse_args()

    result = run(no_cache=args.no_cache)

    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n[main] === 結果摘要（{result['market_date']}）===")
    print(f"[main] Regime: {result['regime']}（廣度={result['breadth_pct']}%, HV20={result['hv20']}%）")
    print(f"[main] Universe {result['universe_count']} → L1 {result['l1_passed_count']} → L2 候選 {result['candidate_count']}")
    print(f"[main] 已寫入 {OUTPUT_PATH}")

    if result["candidates"]:
        print("\n[main] 分數分布（Top 10）：")
        for c in result["candidates"][:10]:
            print(f"  {c['symbol']:>10} {c['total_score']:>6.1f}分  ({c['sector']})")


if __name__ == "__main__":
    main()

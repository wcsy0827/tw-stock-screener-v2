#!/usr/bin/env python3
"""
台股選股系統 — 主程式入口。

Usage:
  python main.py --dry-run
  python main.py --dry-run --no-cache
  python main.py                    # 正式執行：完整跑完 L0~L3 + tracker + 發布報告並 push
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from dotenv import load_dotenv

load_dotenv()

import disposition
import fetcher
import filter as filter_
import market
import publisher
import ranker
import scorer
import tracker
import universe


OUTPUT_PATH = Path(__file__).parent / "data" / "candidates.json"
VIX_HISTORY_PATH = Path(__file__).parent / "data" / "taifex_vix_history.json"


def _record_vix_history(market_date: str, vol_value: float, vix_source: str) -> None:
    """累積波動率訊號歷史（供未來重新校準用），同一 market_date 重跑時覆寫而非重複追加。"""
    history = []
    if VIX_HISTORY_PATH.exists():
        try:
            with open(VIX_HISTORY_PATH, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            history = []

    entry = {"market_date": market_date, "vix_value": vol_value, "vix_source": vix_source}
    if history and history[-1].get("market_date") == market_date:
        history[-1] = entry
    else:
        history.append(entry)

    VIX_HISTORY_PATH.parent.mkdir(exist_ok=True)
    with open(VIX_HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    print(f"[main] 波動率歷史已記錄：{market_date} = {vol_value}%（{vix_source}），累積 {len(history)} 筆")


def run(
    no_cache: bool = False,
    min_score: float = 60.0,
    top_n: int = 3,
    use_ai_cache: bool = True,
) -> dict:
    print("[main] Step 1: Universe shortlist（當日成交金額排序）")
    shortlist_symbols, shortlist_sector_map, directory = universe.fetch_shortlist()
    prev_roster = universe.load_roster()

    # 前次名單存活股即使今天不在 shortlist 內也要納入下載，
    # 否則遲滯判斷會因為根本沒有新數據，被迫誤判為「跌出名單」（見 universe.py 模組說明）
    download_symbols = sorted(set(shortlist_symbols) | set(prev_roster))

    sector_map = dict(shortlist_sector_map)
    for sym in prev_roster:
        if sym not in sector_map:
            sector_map[sym] = universe.sector_for(sym, directory)

    print(f"[main] Step 2: 抓取日 K 數據（shortlist {len(shortlist_symbols)} 支 ∪ 前次名單 {len(prev_roster)} 支 = {len(download_symbols)} 支）")
    fetch_symbols = download_symbols + [market.BENCHMARK_TICKER]

    price_data = None if no_cache else fetcher.load_price_cache()
    if price_data is None:
        price_data = fetcher.fetch_batch(fetch_symbols)
        fetcher.save_price_cache(price_data)

    price_data = fetcher.trim_incomplete_session(price_data)

    if market.BENCHMARK_TICKER not in price_data:
        print(f"[main] 警告：{market.BENCHMARK_TICKER} 下載失敗，Regime/RS fallback 將受影響")

    print("[main] Step 2.3: 30 日均成交金額重排 + 名單遲滯")
    ranked_symbols = universe.rank_by_30d_avg_trade_value(download_symbols, price_data)
    symbols = universe.apply_roster_hysteresis(ranked_symbols, prev_roster)
    sector_map = {s: sector_map.get(s, "Unknown") for s in symbols}
    print(f"[main] 最終名單：{len(symbols)} 支（目標 {universe.TARGET_COUNT}，遲滯帶上限 {universe.HYSTERESIS_BAND}）")

    print("[main] Step 2.5: 簡化 Regime 判定")
    breadth_input = {s: price_data[s] for s in symbols if s in price_data}
    if market.BENCHMARK_TICKER in price_data:
        breadth_input[market.BENCHMARK_TICKER] = price_data[market.BENCHMARK_TICKER]
    regime, breadth_pct, vol_value, vix_source = market.fetch_regime_quick(breadth_input)

    print("[main] Step 3: 抓取基本面資訊")
    info_data = None if no_cache else fetcher.load_info_cache()
    if info_data is None:
        info_data = fetcher.fetch_info(symbols)
        fetcher.save_info_cache(info_data)

    print("[main] Step 3.5: 處置股/分盤集合競價排除名單")
    excluded_symbols = disposition.fetch_excluded_symbols()

    print("[main] Step 4: L1 流動性篩選")
    l1_passed = filter_.apply_filters(
        {s: price_data[s] for s in symbols if s in price_data},
        info_data,
        excluded_symbols=excluded_symbols,
    )

    print("[main] Step 5: L2 技術評分")
    candidates = scorer.score_all(l1_passed, price_data, min_score=min_score, regime=regime, sector_map=sector_map)

    market_date = str(price_data[market.BENCHMARK_TICKER].index[-1].date()) if market.BENCHMARK_TICKER in price_data else str(date.today())
    universe.save_roster(symbols, market_date)
    _record_vix_history(market_date, vol_value, vix_source)

    result: dict = {
        "market_date": market_date,
        "regime": regime,
        "breadth_pct": breadth_pct,
        "vix_value": vol_value,
        "vix_source": vix_source,
        "universe_count": len(symbols),
        "l1_passed_count": len(l1_passed),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }

    if not candidates:
        print("[main] 無候選股，跳過 L3/tracker/發布")
        result["ranked"] = []
        result["market_context"] = {}
        return result

    print("[main] Step 5.5: 組裝大盤背景（供 L3 Prompt 與報告儀表板）")
    candidate_sectors = {c["sector"] for c in candidates if c.get("sector") and c["sector"] != "Unknown"}
    market_context = market.fetch_market_context(
        all_stocks_data=price_data,
        sector_map=sector_map,
        candidate_sectors=candidate_sectors,
        breadth_pct=breadth_pct,
        vol_value=vol_value,
        vix_source=vix_source,
    )
    result["market_context"] = market_context

    print("[main] Step 6: L3 AI 精選")
    ranked_out = ranker.rank_candidates(
        candidates, price_data, info_data,
        top_n=top_n, market_context=market_context,
        market_date=market_date, use_ai_cache=use_ai_cache,
        sector_map=sector_map,
    )
    result["ranked"] = ranked_out
    print(f"[main] L3 精選完成，{len(ranked_out)} 支買入候選")

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="台股選股系統")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="輸出候選池 JSON 並在本機生成 HTML 報告，但不 push 至 GitHub",
    )
    parser.add_argument("--no-cache", action="store_true", help="忽略快取，強制重新下載（同時略過 AI 快取）")
    parser.add_argument(
        "--no-ai-cache", action="store_true",
        help="忽略 AI 快取，強制重新呼叫 DeepSeek（price/info 快取仍複用）",
    )
    parser.add_argument(
        "--top", type=int, default=int(os.getenv("MAX_OUTPUT", "3")), metavar="N",
        help="L3 輸出幾支候選股（預設 3）",
    )
    parser.add_argument(
        "--min-score", type=float, default=float(os.getenv("MIN_SCORE", "60")), metavar="N",
        help="L2 最低評分門檻（預設 60）",
    )
    parser.add_argument("--yes", action="store_true", help="跳過今日重複執行確認（CI 環境用）")
    args = parser.parse_args()

    if tracker.check_already_run_today() and not args.yes:
        print("\n⚠️  今日已執行過追蹤，再次執行不會增加追蹤天數。")
        try:
            confirm = input("是否繼續？(y/N) ").strip().lower()
        except EOFError:
            confirm = "n"
        if confirm != "y":
            print("已取消。")
            sys.exit(0)

    result = run(
        no_cache=args.no_cache,
        min_score=args.min_score,
        top_n=args.top,
        use_ai_cache=not args.no_cache and not args.no_ai_cache,
    )

    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {k: v for k, v in result.items() if k not in ("ranked", "market_context")},
            f, ensure_ascii=False, indent=2,
        )

    print(f"\n[main] === 結果摘要（{result['market_date']}）===")
    print(f"[main] Regime: {result['regime']}（廣度={result['breadth_pct']}%, 波動率={result['vix_value']}%[{result['vix_source']}]）")
    print(f"[main] Universe {result['universe_count']} → L1 {result['l1_passed_count']} → L2 候選 {result['candidate_count']} → L3 精選 {len(result.get('ranked', []))}")
    print(f"[main] 已寫入 {OUTPUT_PATH}")

    if result["candidates"]:
        print("\n[main] 分數分布（Top 10）：")
        for c in result["candidates"][:10]:
            print(f"  {c['symbol']:>10} {c['total_score']:>6.1f}分  ({c['sector']})")

    ranked = result.get("ranked", [])
    market_context = result.get("market_context", {})
    market_date_str = result.get("market_date")

    # Phase 3 P6 guard：tracker 的結算是不可逆歸檔動作，僅在收盤後且交易日執行；
    # 非安全時段仍完整輸出上方 L0~L3 結果，只跳過 tracker/發布這一段（見
    # docs/phase3_limit_lock_design.md P6、tracker.is_safe_to_run()）。
    if not tracker.is_safe_to_run():
        print("\n[main] ⚠️  目前非收盤後/非交易日，跳過 tracker 追蹤與報告發布"
              "（避免用殘缺/重複 bar 做漲跌停判定並不可逆歸檔）")
        return

    _, categories = tracker.run_tracker(ranked, market_context=market_context, market_date=market_date_str)

    market_date_dt = datetime.strptime(market_date_str, "%Y-%m-%d") if market_date_str else datetime.utcnow()
    stats = {
        "total":    result.get("universe_count", 0),
        "l1_count": result.get("l1_passed_count", 0),
        "l2_count": result.get("candidate_count", 0),
        # fallback（AI 排序失敗時的 L2 分數退化輸出）不算真正的 AI 精選
        "ai_count": sum(1 for r in ranked if not r.get("is_fallback")),
        "date":     market_date_dt,
    }
    publisher.publish(categories, stats, dry_run=args.dry_run, market_context=market_context)


if __name__ == "__main__":
    main()

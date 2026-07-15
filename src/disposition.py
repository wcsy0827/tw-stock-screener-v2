"""排除目前處於 TWSE 處置或更嚴重交易限制狀態的股票（補進 L1，見 TODO.md）。

依據（TWSE OpenAPI，皆為官方每日更新的現況公告，非歷史封存）：
- 處置股（disposition stocks）：`/v1/announcement/punish`「集中市場公布處置股票」，
  含 `DispositionPeriod`（處置起迄時間，民國曆，如 "115/07/03～115/07/16"）。
  只排除「今天仍落在處置期間內」的股票，處置期滿後下次抓取自然解除排除，
  不需要另外維護到期清單。
- 全額交割股：TWSE OpenAPI 未提供獨立的「全額交割股現況」清單（詢問 swagger
  全部 143 個 endpoint 後確認），故以 `/v1/exchangeReport/TWT85U`「集中市場證券
  變更交易」的 `PeriodicCallAuctionTrading`（分盤集合競價）欄位作為代理指標
  （設計文件/TODO.md 原文即為「TWSE 處置公告或代理指標」）：分盤集合競價是比
  一般處置更嚴重的交易限制措施，非空白即代表目前處於此狀態，一律排除。

兩份清單皆為「當下狀態」快照，呼叫端（main.py）應在每次執行時重新抓取，
不可跨日快取；任一清單抓取失敗時回傳空字典並印出警告，不中斷主流程
（沿用 universe.py／fetcher.py 既有的「抓取失敗則優雅降級」慣例）。
"""

from __future__ import annotations

import re
from datetime import date

import requests

PUNISH_URL = "https://openapi.twse.com.tw/v1/announcement/punish"
TRADING_METHOD_CHANGE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/TWT85U"

# 民國曆日期區間，如 "115/07/03～115/07/16"（分隔符實際為全形波浪號，
# 但用 \D+ 泛用匹配任何非數字分隔符，不假設特定符號）
_PERIOD_RE = re.compile(r"(\d{2,3})/(\d{1,2})/(\d{1,2})\D+(\d{2,3})/(\d{1,2})/(\d{1,2})")


def _parse_roc_period(period_str: str) -> tuple[date, date] | None:
    """解析民國曆日期區間字串 → (start, end)。格式不符時回傳 None（呼叫端視為
    無法判斷是否仍在處置期間，不排除——寧可漏排除也不因解析失敗誤刪整批名單）。"""
    m = _PERIOD_RE.search(period_str or "")
    if not m:
        return None
    y1, m1, d1, y2, m2, d2 = m.groups()
    try:
        start = date(int(y1) + 1911, int(m1), int(d1))
        end = date(int(y2) + 1911, int(m2), int(d2))
        return start, end
    except ValueError:
        return None


def fetch_active_disposition_symbols(today: date | None = None) -> dict[str, str]:
    """回傳 {symbol(.TW): reason}：目前仍在處置期間內的股票（處置期滿自動解除）。"""
    today = today or date.today()
    try:
        resp = requests.get(PUNISH_URL, timeout=30)
        resp.raise_for_status()
        rows = resp.json()
    except Exception as e:
        print(f"[disposition] 處置股清單抓取失敗，本次不排除：{e}")
        return {}

    excluded: dict[str, str] = {}
    for row in rows:
        code = (row.get("Code") or "").strip()
        if not code:
            continue
        period = _parse_roc_period(row.get("DispositionPeriod", ""))
        if period is None:
            continue
        start, end = period
        if start <= today <= end:
            measures = row.get("DispositionMeasures", "")
            excluded[f"{code}.TW"] = f"處置股（{measures}，{row.get('DispositionPeriod', '')}）"

    print(f"[disposition] 處置股公告：{len(rows)} 筆，目前仍在處置期間內 {len(excluded)} 支")
    return excluded


def fetch_batch_auction_symbols() -> dict[str, str]:
    """回傳 {symbol(.TW): reason}：目前處於分盤集合競價狀態的股票
    （全額交割等更嚴重交易限制的代理指標，見模組說明）。"""
    try:
        resp = requests.get(TRADING_METHOD_CHANGE_URL, timeout=30)
        resp.raise_for_status()
        rows = resp.json()
    except Exception as e:
        print(f"[disposition] 變更交易清單抓取失敗，本次不排除：{e}")
        return {}

    excluded: dict[str, str] = {}
    for row in rows:
        code = (row.get("Code") or "").strip()
        marker = (row.get("PeriodicCallAuctionTrading") or "").strip()
        if code and marker:
            excluded[f"{code}.TW"] = "分盤集合競價（全額交割等更嚴重交易限制的代理指標）"

    print(f"[disposition] 變更交易清單：{len(rows)} 支，分盤集合競價中 {len(excluded)} 支")
    return excluded


def fetch_excluded_symbols(today: date | None = None) -> dict[str, str]:
    """回傳 {symbol(.TW): reason} 聯集：目前應排除的處置股 + 分盤集合競價股。
    同一 symbol 兩份清單皆命中時保留處置股的原因字串（先寫入者優先）。"""
    excluded = fetch_active_disposition_symbols(today)
    for symbol, reason in fetch_batch_auction_symbols().items():
        excluded.setdefault(symbol, reason)
    return excluded

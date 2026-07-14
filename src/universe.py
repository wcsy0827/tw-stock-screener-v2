"""
從 TWSE OpenAPI 取得上市公司清單，依 30 日均成交金額近似篩出台灣50+中型100範圍的候選股，
搭配名單遲滯（roster hysteresis）避免每日換血。

流程（兩段式，見 main.py 呼叫順序）：
  1. fetch_shortlist()          — 依「當日」成交金額排序取前 SHORTLIST_COUNT 支（成本可控的下載候選池）
  2. （main.py）下載 shortlist ∪ 前次名單存活股 的 90 日 K 線
  3. rank_by_30d_avg_trade_value() — 用剛下載的數據算 30 日均成交金額，重新排序
  4. apply_roster_hysteresis()   — 已在前次名單的股票，除非跌出前 HYSTERESIS_BAND 名才移除

第 2 步刻意把「前次名單存活股」也納入下載，即使它們今天不在 shortlist 內（例如單日
成交量剛好偏低）——否則遲滯判斷會因為根本沒有新數據可用，被迫誤判為「跌出名單」，
而非真的跌出 30 日均量的 HYSTERESIS_BAND。
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pandas as pd
import requests

COMPANY_LIST_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
DAILY_QUOTE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
ISIN_LIST_URL = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2"

SHORTLIST_COUNT = 250
TARGET_COUNT = 150
HYSTERESIS_BAND = 180

_STOCK_CODE_RE = re.compile(r"^[1-9]\d{3}$")  # 4位數字且不以0開頭，排除ETF(00xx)/權證
_ROSTER_PATH = Path(__file__).parent.parent / "data" / "universe_roster.json"


def _is_ordinary_stock(code: str) -> bool:
    return bool(_STOCK_CODE_RE.match(code))


def fetch_industry_names() -> dict[str, str]:
    """
    從 TWSE ISIN 公開資訊查詢頁解析 {代號: 產業別中文名稱}（僅市場別=上市）。

    比 t187ap03_L 的「產業別」欄位（實測回傳兩位數代碼如 "01"，不是名稱文字）更易讀，
    供候選池顯示與 scorer 的 RS 同產業分組使用。頁面編碼為 big5hkscs（非 UTF-8），
    抓取失敗時回傳空字典，呼叫端 fallback 為 t187ap03_L 的數字代碼，不中斷流程。
    """
    try:
        resp = requests.get(ISIN_LIST_URL, timeout=30)
        resp.raise_for_status()
        html = resp.content.decode("big5hkscs", errors="replace")
        df = pd.read_html(io.StringIO(html))[0]
    except Exception as e:
        print(f"[universe] ISIN 產業別名稱抓取失敗，改用 t187ap03_L 代碼：{e}")
        return {}

    df.columns = ["code_name", "isin", "listed_date", "market", "industry", "cfi", "note"]
    df = df.iloc[2:]  # 前兩列是重複的標題列
    df = df[df["market"] == "上市"]

    names: dict[str, str] = {}
    for _, row in df.iterrows():
        code_name = str(row["code_name"])
        code = code_name.split("　")[0].strip()  # 全形空白分隔代號與名稱
        if _is_ordinary_stock(code):
            names[code] = str(row["industry"]).strip()
    print(f"[universe] ISIN 產業別名稱：{len(names)} 支上市股")
    return names


def fetch_company_directory() -> dict[str, dict]:
    """回傳 {code: {"name":, "industry":}}；industry 優先取 ISIN 頁面中文名稱，缺失時 fallback 為代碼。"""
    resp = requests.get(COMPANY_LIST_URL, timeout=30)
    resp.raise_for_status()
    rows = resp.json()

    industry_names = fetch_industry_names()

    directory: dict[str, dict] = {}
    for row in rows:
        code = row.get("公司代號", "").strip()
        if not _is_ordinary_stock(code):
            continue
        fallback_code = row.get("產業別", "Unknown") or "Unknown"
        directory[code] = {
            "name": row.get("公司簡稱", code),
            "industry": industry_names.get(code, fallback_code),
        }
    print(f"[universe] TWSE 上市公司清單：{len(directory)} 支普通股")
    return directory


def fetch_daily_trade_value() -> dict[str, float]:
    """回傳 {code: 當日成交金額}，用於 shortlist 初篩排序（低成本，僅一次 API 呼叫）。"""
    resp = requests.get(DAILY_QUOTE_URL, timeout=30)
    resp.raise_for_status()
    rows = resp.json()

    trade_value: dict[str, float] = {}
    for row in rows:
        code = row.get("Code", "").strip()
        if not _is_ordinary_stock(code):
            continue
        try:
            trade_value[code] = float(row.get("TradeValue", 0) or 0)
        except ValueError:
            continue
    return trade_value


def fetch_shortlist(count: int = SHORTLIST_COUNT) -> tuple[list[str], dict[str, str], dict[str, dict]]:
    """
    依「當日」成交金額排序，取前 count 支作為下載候選池（成本可控，非最終名單）。
    最終名單由 main.py 呼叫 rank_by_30d_avg_trade_value + apply_roster_hysteresis 決定。

    回傳 (symbols, sector_map, directory)：directory 供呼叫端查詢「前次名單存活股」
    （可能不在本次 shortlist 內）的產業別。
    """
    directory = fetch_company_directory()
    trade_value = fetch_daily_trade_value()

    ranked_codes = sorted(
        (code for code in directory if code in trade_value),
        key=lambda c: trade_value[c],
        reverse=True,
    )[:count]

    symbols = [f"{code}.TW" for code in ranked_codes]
    sector_map = {f"{code}.TW": directory[code]["industry"] for code in ranked_codes}

    print(f"[universe] 當日成交金額 shortlist：{len(symbols)} 支（下載候選池，目標 {count} 支）")
    return symbols, sector_map, directory


def sector_for(symbol: str, directory: dict[str, dict]) -> str:
    """從 directory 查詢單一 symbol 的產業別，供組合前次名單存活股的 sector_map 使用。"""
    code = symbol.split(".")[0]
    return directory.get(code, {}).get("industry", "Unknown")


def rank_by_30d_avg_trade_value(symbols: list[str], price_data: dict) -> list[str]:
    """依 30 日平均成交金額（收盤價×成交量）降冪排序，取代單日排序的雜訊。"""
    scored: list[tuple[str, float]] = []
    for sym in symbols:
        df = price_data.get(sym)
        if df is None or df.empty:
            continue
        close = df["Close"].dropna()
        volume = df["Volume"].dropna()
        if close.empty or volume.empty:
            continue
        n = min(30, len(close), len(volume))
        avg_trade_value = float((close.tail(n) * volume.tail(n)).mean())
        scored.append((sym, avg_trade_value))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in scored]


def apply_roster_hysteresis(
    ranked_symbols: list[str],
    prev_symbols: list[str],
    target_count: int = TARGET_COUNT,
    band: int = HYSTERESIS_BAND,
) -> list[str]:
    """
    名單遲滯（banding，比照 Russell 重構慣例）：已在前次名單的股票，除非排名跌出前
    band 名，否則保留；名單未滿 target_count 時才依排名遞補新股。

    允許最終名單介於 target_count ~ band 之間（不強制縮回 target_count），
    用些微超出目標區間換取名單穩定性，避免 tracker 未來追蹤的條目每日被換血打斷。
    """
    rank_of = {s: i for i, s in enumerate(ranked_symbols)}
    survivors = [s for s in prev_symbols if rank_of.get(s, 10**9) < band]
    survivor_set = set(survivors)

    result = list(survivors)
    for s in ranked_symbols:
        if len(result) >= target_count:
            break
        if s not in survivor_set:
            result.append(s)
    return result


def load_roster() -> list[str]:
    """讀取前次名單，不存在或損壞時回傳空列表（首次執行的正常狀態）。"""
    if not _ROSTER_PATH.exists():
        return []
    try:
        with open(_ROSTER_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("symbols", [])
    except Exception as e:
        print(f"[universe] 名單讀取失敗，視為首次執行：{e}")
        return []


def save_roster(symbols: list[str], market_date: str) -> None:
    _ROSTER_PATH.parent.mkdir(exist_ok=True)
    with open(_ROSTER_PATH, "w", encoding="utf-8") as f:
        json.dump({"market_date": market_date, "symbols": symbols}, f, ensure_ascii=False, indent=2)
    print(f"[universe] 名單已儲存：{len(symbols)} 支（{market_date}）")


if __name__ == "__main__":
    syms, sectors, _directory = fetch_shortlist()
    print(syms[:10])
    for s in syms[:5]:
        print(s, sectors[s])

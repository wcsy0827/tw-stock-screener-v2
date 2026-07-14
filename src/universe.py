"""從 TWSE OpenAPI 取得上市公司清單，依成交金額近似篩出台灣50+中型100範圍的候選股。"""

from __future__ import annotations

import re

import requests

COMPANY_LIST_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
DAILY_QUOTE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"

TARGET_COUNT = 150
_STOCK_CODE_RE = re.compile(r"^[1-9]\d{3}$")  # 4位數字且不以0開頭，排除ETF(00xx)/權證


def _is_ordinary_stock(code: str) -> bool:
    return bool(_STOCK_CODE_RE.match(code))


def fetch_company_directory() -> dict[str, dict]:
    """回傳 {code: {"name": 公司名稱, "industry": 產業別}}，僅含上市普通股。"""
    resp = requests.get(COMPANY_LIST_URL, timeout=30)
    resp.raise_for_status()
    rows = resp.json()

    directory: dict[str, dict] = {}
    for row in rows:
        code = row.get("公司代號", "").strip()
        if not _is_ordinary_stock(code):
            continue
        directory[code] = {
            "name": row.get("公司簡稱", code),
            "industry": row.get("產業別", "Unknown") or "Unknown",
        }
    print(f"[universe] TWSE 上市公司清單：{len(directory)} 支普通股")
    return directory


def fetch_daily_trade_value() -> dict[str, float]:
    """回傳 {code: 當日成交金額}，用作市值/流動性排序的近似代理指標。"""
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


def fetch_universe(target_count: int = TARGET_COUNT) -> tuple[list[str], dict[str, dict]]:
    """
    近似篩出「台灣50＋中型100」範圍的候選股（依當日成交金額排序取前 target_count 支）。

    不追求與 0050/0051 官方成分股逐一對齊（那是 FTSE 方法論下的 ETF 成分股，非公開 API），
    MVP 目的是驗證資料管線與 L2 分數分布，用成交金額排序近似大型/中型股範圍已足夠。
    僅涵蓋 TWSE 上市（.TW），未涵蓋 TPEx 上櫃（.TWO）。

    回傳 (symbols, sector_map)：
      symbols    — [".TW" 後綴代號]，依成交金額降冪排列
      sector_map — {".TW"代號: 產業別}，供 scorer 的 RS 維度使用
    """
    directory = fetch_company_directory()
    trade_value = fetch_daily_trade_value()

    ranked_codes = sorted(
        (code for code in directory if code in trade_value),
        key=lambda c: trade_value[c],
        reverse=True,
    )[:target_count]

    symbols = [f"{code}.TW" for code in ranked_codes]
    sector_map = {f"{code}.TW": directory[code]["industry"] for code in ranked_codes}

    print(f"[universe] 依成交金額近似篩出 {len(symbols)} 支候選股（目標 {target_count} 支）")
    return symbols, sector_map


if __name__ == "__main__":
    syms, sectors = fetch_universe()
    print(syms[:10])
    for s in syms[:5]:
        print(s, sectors[s])

"""抓取 TAIFEX「臺指選擇權波動率指數」（台股真 VIX 等價物）最新值。

資料來源：https://www.taifex.com.tw/file/taifex/Dailydownload/vix/log2data/{YYYYMM}new.txt
（不需登入、不需 POST 表單，單純 GET 純文字檔，Big5 編碼、Tab 分隔）。

已知限制：這個 endpoint 只保留約 3~4 個月的近期資料（實測 2026-07 執行時，
2026-04 有資料、2026-03 以前一律回傳偽裝成 200 的 404 頁面），不是深度歷史
archive，無法比照 ^TWII HV20 做長期分位數校準。見 market.py 模組說明。
"""

from __future__ import annotations

from datetime import date

import requests

_BASE_URL = "https://www.taifex.com.tw/file/taifex/Dailydownload/vix/log2data/{ym}new.txt"


def _fetch_month_file(year: int, month: int) -> str | None:
    """下載單月資料檔，回傳解碼後文字；下載失敗或該月無資料（偽裝404）回傳 None。"""
    ym = f"{year:04d}{month:02d}"
    try:
        resp = requests.get(_BASE_URL.format(ym=ym), timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"[taifex_vix] {ym} 下載失敗：{e}")
        return None

    content = resp.content
    if content[:10].lstrip().startswith(b"<HTML") or content[:10].lstrip().startswith(b"<html"):
        # TAIFEX 對不存在的月份回傳 200 + 偽裝的 404 頁面，不是真的請求失敗
        return None
    try:
        return content.decode("big5", errors="replace")
    except Exception as e:
        print(f"[taifex_vix] {ym} 解碼失敗：{e}")
        return None


def _parse_latest_value(text: str) -> float | None:
    """從月檔文字取最後一列的「臺指選擇權波動率指數」欄位值。"""
    lines = [ln for ln in text.strip().split("\n") if ln.strip()]
    for line in reversed(lines):
        parts = [p for p in line.split("\t") if p.strip()]
        if len(parts) < 3:
            continue
        date_str, value_str = parts[0].strip(), parts[2].strip()
        if not date_str.isdigit() or len(date_str) != 8:
            continue
        try:
            return float(value_str)
        except ValueError:
            continue
    return None


def fetch_latest_vix(market_date: date | None = None) -> float | None:
    """
    取得最新交易日的臺指選擇權波動率指數。優先抓 market_date 所在月份的檔案，
    若該月檔案尚無資料（例如月初第一個交易日執行時，理論上不會發生但保留防呆）
    則 fallback 上個月。全程失敗回傳 None，呼叫端應 fallback 為 HV20（見 market.py）。
    """
    d = market_date or date.today()

    text = _fetch_month_file(d.year, d.month)
    if text is not None:
        value = _parse_latest_value(text)
        if value is not None:
            return value

    prev_year, prev_month = (d.year - 1, 12) if d.month == 1 else (d.year, d.month - 1)
    text = _fetch_month_file(prev_year, prev_month)
    if text is not None:
        value = _parse_latest_value(text)
        if value is not None:
            return value

    print("[taifex_vix] 當月與上月皆無可用資料")
    return None


if __name__ == "__main__":
    v = fetch_latest_vix()
    print(f"最新臺指選擇權波動率指數：{v}")

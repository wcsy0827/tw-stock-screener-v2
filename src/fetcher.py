"""用 yfinance 批次抓取台股近 90 天日 K 數據與基本面資訊。"""

from __future__ import annotations

import json
import pickle
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf
import pandas as pd


BATCH_SIZE = 50
PERIOD = "90d"
INTERVAL = "1d"
MAX_RETRIES = 3
RETRY_DELAY = 5
MARKET_CLOSE_HOUR = 13
MARKET_CLOSE_MINUTE = 45  # 台股 13:30 收盤 + 15 分鐘 settle buffer
MIN_BARS = 20  # 與 fetch_batch 的最低列數門檻一致
BENCHMARK_TICKER = "^TWII"

_CACHE_DIR = Path(__file__).parent.parent / ".cache"


# ── 快取工具 ─────────────────────────────────────────────────────

def _today() -> str:
    return date.today().strftime("%Y%m%d")


def _price_cache_path(date_str: str) -> Path:
    return _CACHE_DIR / f"price_{date_str}.pkl"


def _info_cache_path(date_str: str) -> Path:
    return _CACHE_DIR / f"info_{date_str}.json"


def load_price_cache(date_str: str | None = None) -> dict[str, pd.DataFrame] | None:
    path = _price_cache_path(date_str or _today())
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            data = pickle.load(f)
        print(f"[cache] 讀取 price 快取：{path.name}（{len(data)} 支）")
        return data
    except Exception as e:
        print(f"[cache] price 快取讀取失敗，重新下載：{e}")
        return None


def save_price_cache(data: dict[str, pd.DataFrame], date_str: str | None = None) -> None:
    _CACHE_DIR.mkdir(exist_ok=True)
    path = _price_cache_path(date_str or _today())
    with open(path, "wb") as f:
        pickle.dump(data, f)
    print(f"[cache] price 快取已儲存：{path.name}")


def load_info_cache() -> dict[str, dict] | None:
    if not _CACHE_DIR.exists():
        return None
    today_ord = date.today().toordinal()
    candidates = []
    for f in _CACHE_DIR.glob("info_*.json"):
        try:
            ds = f.stem.split("_")[1]
            y, m, d_ = int(ds[:4]), int(ds[4:6]), int(ds[6:])
            age = today_ord - date(y, m, d_).toordinal()
            if 0 <= age <= 7:
                candidates.append((age, f))
        except Exception:
            pass
    if not candidates:
        return None
    _, path = min(candidates, key=lambda x: x[0])
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        print(f"[cache] 讀取 info 快取：{path.name}（{len(data)} 支）")
        return data
    except Exception as e:
        print(f"[cache] info 快取讀取失敗，重新下載：{e}")
        return None


def save_info_cache(data: dict[str, dict]) -> None:
    _CACHE_DIR.mkdir(exist_ok=True)
    path = _info_cache_path(_today())
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[cache] info 快取已儲存：{path.name}")


# ── 下載函式 ─────────────────────────────────────────────────────

def _download_with_retry(tickers: list[str], **kwargs) -> pd.DataFrame:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return yf.download(tickers=tickers, **kwargs)
        except Exception as e:
            if attempt == MAX_RETRIES:
                raise
            print(f"[fetcher] 下載失敗（第{attempt}次），{RETRY_DELAY}秒後重試：{e}")
            time.sleep(RETRY_DELAY * attempt)
    return pd.DataFrame()


def fetch_batch(symbols: list[str]) -> dict[str, pd.DataFrame]:
    """批次下載 OHLCV 日線數據，回傳 {symbol: DataFrame}。"""
    result: dict[str, pd.DataFrame] = {}

    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i : i + BATCH_SIZE]
        print(f"[fetcher] 下載 {i+1}~{min(i+BATCH_SIZE, len(symbols))} / {len(symbols)}")

        try:
            raw = _download_with_retry(
                tickers=batch,
                period=PERIOD,
                interval=INTERVAL,
                group_by="ticker",
                auto_adjust=True,
                progress=False,
                threads=True,
            )
        except Exception as e:
            print(f"[fetcher] 批次下載失敗，跳過：{e}")
            continue

        for sym in batch:
            try:
                df = raw[sym].copy() if isinstance(raw.columns, pd.MultiIndex) else raw.copy()
                df = df.dropna(how="all")
                if len(df) >= 20:
                    result[sym] = df
            except Exception:
                pass

        if i + BATCH_SIZE < len(symbols):
            time.sleep(1)

    print(f"[fetcher] 成功取得 {len(result)} 支股票數據")
    return result


def trim_incomplete_session(price_data: dict[str, pd.DataFrame], now: datetime | None = None) -> dict[str, pd.DataFrame]:
    """捨棄尚未收盤的殘缺當日 K 棒（台股 13:30 收盤 + 15 分鐘 settle buffer）。"""
    benchmark_df = price_data.get(BENCHMARK_TICKER)
    if benchmark_df is None or benchmark_df.empty:
        return price_data

    tz = ZoneInfo("Asia/Taipei")
    now_local = (now or datetime.now(tz)).astimezone(tz)
    last_date = benchmark_df.index[-1].date()

    close_cutoff = now_local.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MINUTE, second=0, microsecond=0)
    if last_date != now_local.date() or now_local >= close_cutoff:
        return price_data

    affected = 0
    dropped = 0
    trimmed: dict[str, pd.DataFrame] = {}
    for sym, df in price_data.items():
        if df.empty or df.index[-1].date() != last_date:
            trimmed[sym] = df
            continue
        affected += 1
        df = df[df.index.map(lambda ts: ts.date()) != last_date]
        if len(df) >= MIN_BARS:
            trimmed[sym] = df
        else:
            dropped += 1

    print(f"[fetcher] 偵測到 {last_date} 尚未收盤，已捨棄殘缺K棒（{affected} 支股票受影響，{dropped} 支因列數不足被移除）")
    return trimmed


def fetch_info(symbols: list[str]) -> dict[str, dict]:
    """抓取股票基本面資訊（市值、產業、公司名稱等）。"""
    info_map: dict[str, dict] = {}

    for i, sym in enumerate(symbols):
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                ticker = yf.Ticker(sym)
                info = ticker.info
                info_map[sym] = {
                    "market_cap": info.get("marketCap"),
                    "sector": info.get("sector", "Unknown"),
                    "name": info.get("shortName") or info.get("longName") or sym,
                    "forward_pe": info.get("forwardPE") or info.get("trailingPE"),
                    "profit_margin": info.get("profitMargins"),
                    "revenue_growth": info.get("revenueGrowth"),
                    "short_percent_float": info.get("shortPercentOfFloat"),
                }
                break
            except Exception:
                if attempt == MAX_RETRIES:
                    info_map[sym] = {"market_cap": None, "sector": "Unknown", "name": sym}
                else:
                    time.sleep(RETRY_DELAY)

        if (i + 1) % 50 == 0:
            print(f"[fetcher] info {i+1}/{len(symbols)}")
            time.sleep(1)

    return info_map


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from universe import fetch_universe

    symbols, _ = fetch_universe()
    data = fetch_batch(symbols[:10])
    info = fetch_info(list(data.keys()))
    for sym, df in list(data.items())[:3]:
        print(f"{sym} ({info[sym]['name']}): {df['Close'].iloc[-1]:.2f}, {info[sym]['sector']}")

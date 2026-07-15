"""訊號追蹤模組：追蹤選股結果是否已落入買入區間，並於觸發條件時結算歸檔。

移植自 `D:\\us-stock-screener\\src\\tracker.py`，並依
`docs/phase3_limit_lock_design.md`（v10 定稿）加上台股 ±10% 漲跌停止損模擬機制：
美股版 `today_low <= stop_loss` 即視為止損成交的假設，在台股跌停鎖死（賣單堆積、
無人接手）時不成立，直接移植會系統性把「連續跌停」的真實虧損記成「-8% 停損出場」，
污染 `performance_history.json` 且不可回溯修復。詳細判定邏輯與抗辯歷程見設計文件。
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

_DATA_DIR        = Path(__file__).parent.parent / "data"
_WATCHLIST_PATH  = _DATA_DIR / "watchlist.json"
_PERF_PATH       = _DATA_DIR / "performance_history.json"

MIN_AI_CONFIDENCE  = int(os.getenv("MIN_AI_CONFIDENCE", "6"))  # AI 信心分數最低門檻
MAX_ACTIVE_POSITIONS = int(os.getenv("MAX_ACTIVE_POSITIONS", "5"))  # 同時持倉上限（槽位制）
_DEFAULT_WATCH_DAYS = 5                    # 突破/動能策略 watch 上限
_WATCH_DAYS_BY_STRATEGY: dict[str, int] = {
    "反轉策略": 10,                         # 底部確認需更長時間
}
_DEFAULT_HOLD_DAYS = 10     # hold_period 無法解析時的預設持倉天數

# 結算原因常數
EXIT_PROFIT   = "CLOSED_PROFIT"
EXIT_LOSS     = "CLOSED_LOSS"
EXIT_TRAILING = "CLOSED_TRAILING_STOP"
EXIT_EXPIRED  = "FORCE_EXPIRED"

# 風控常數
BREAKEVEN_PROFIT_THRESHOLD = 0.5    # 達目標距離 50% 時觸發保本
TRAILING_ACTIVATION_PCT    = 0.10   # 峰值浮盈需超過 10% 才啟動移動停利
TRAILING_RETRACE_PCT       = 0.05   # 從峰值收盤回撤 5% 觸發出場

# ── 漲跌停鎖死判定常數（Phase 3，見 docs/phase3_limit_lock_design.md §3）──
LOCK_VOLUME_RATIO = 0.3   # 暫定值，上線前須跑 scripts/calibrate_lock.py 校準（§3.1 R2）
PENDING_STALE_LIMIT = 15  # 連續無新鮮資料達此門檻 → 強制結算（§4.5）

MARKET_CLOSE_HOUR = 13
MARKET_CLOSE_MINUTE = 45  # 台股 13:30 收盤 + 15 分鐘 settle buffer（同 fetcher.trim_incomplete_session）


# ── I/O ─────────────────────────────────────────────────────────────

def load_watchlist() -> list[dict]:
    if not _WATCHLIST_PATH.exists():
        return []
    try:
        with open(_WATCHLIST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[tracker] watchlist 讀取失敗：{e}")
        return []


def save_watchlist(watchlist: list[dict]) -> None:
    """原子寫入（tmp + replace），防止寫入中途崩潰導致 JSON 損壞（設計 §6）。"""
    _DATA_DIR.mkdir(exist_ok=True)
    tmp_path = _WATCHLIST_PATH.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(watchlist, f, ensure_ascii=False, indent=2)
    tmp_path.replace(_WATCHLIST_PATH)


def check_already_run_today() -> bool:
    """檢查今日（UTC）是否已執行過追蹤，回傳 True 表示已執行。
    使用 UTC 日期確保與 CI 環境行為一致（market_date 以 UTC 為基準）。"""
    today_utc = datetime.utcnow().date().isoformat()
    watchlist = load_watchlist()
    return any(today_utc in e.get("tracked_dates", []) for e in watchlist)


def is_safe_to_run(now: datetime | None = None) -> bool:
    """執行前置 guard（設計 P6）：僅收盤後（13:45 台北時間後）且僅在交易日執行。

    交易日判定僅排除週末（同母本 `_count_trading_days` 的既有限制，不排除法定
    假日），完整 TWSE 交易日曆判斷留待未來排程基礎設施（見 TODO.md「GitHub
    遠端/Actions 排程」）建立時補強。呼叫端（未來的排程/main.py 整合）應在
    呼叫 `run_tracker` 前先檢查此 guard，避免盤中或非交易日誤跑，用殘缺/重複
    bar 做鎖死判定並不可逆歸檔。
    """
    tz = ZoneInfo("Asia/Taipei")
    now_local = (now or datetime.now(tz)).astimezone(tz)
    if now_local.weekday() >= 5:  # 週六=5, 週日=6
        return False
    close_cutoff = now_local.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MINUTE, second=0, microsecond=0)
    return now_local >= close_cutoff


# ── 工具函式 ─────────────────────────────────────────────────────────

def _parse_hold_period(hold_period_str, default: int = _DEFAULT_HOLD_DAYS) -> int:
    """解析 hold_period 為整數天數。接受 int/float 直接回傳，或從字串萃取最大數值。
    下界固定為 1：AI 若給出 <=0 的異常值，同日觸價成交（active_days 首輪即為 1）
    會被誤判為 FORCE_EXPIRED，故無條件夾在最小值 1。"""
    if isinstance(hold_period_str, int):
        return max(1, hold_period_str)
    if isinstance(hold_period_str, float):
        return max(1, int(hold_period_str))
    s = str(hold_period_str) if hold_period_str is not None else ""
    if not s or s.strip() in ("-", ""):
        return default
    nums = re.findall(r"\d+", s)
    if not nums:
        return default
    return max(1, max(int(n) for n in nums))


def _count_trading_days(start: str, end: str) -> int:
    """計算兩日期間的交易日數（僅計週一至週五，不排除法定假日）。"""
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    return sum(1 for i in range((d1 - d0).days) if (d0 + timedelta(days=i)).weekday() < 5)


def _parse_stop_loss(stop_loss_str: str) -> float | None:
    """解析 "$182.50" 或 "182" → 182.5，失敗回傳 None。"""
    if not stop_loss_str or stop_loss_str.strip() in ("-", ""):
        return None
    nums = re.findall(r"[\d,]+\.?\d*", stop_loss_str)
    if not nums:
        return None
    try:
        return float(nums[0].replace(",", ""))
    except ValueError:
        return None


def _parse_target(target_str: str) -> float | None:
    """解析 "$210" 或 "210" → 210.0，失敗回傳 None。"""
    return _parse_stop_loss(target_str)


def _parse_buy_zone(buy_zone_str: str) -> tuple[float, float] | None:
    """解析 "$185～$188" → (185.0, 188.0)，失敗回傳 None。"""
    if not buy_zone_str or buy_zone_str.strip() in ("-", ""):
        return None
    nums = re.findall(r"[\d,]+\.?\d*", buy_zone_str)
    if len(nums) < 2:
        return None
    try:
        low = float(nums[0].replace(",", ""))
        high = float(nums[1].replace(",", ""))
        return (low, high) if low <= high else (high, low)
    except ValueError:
        return None


def _open_or_close(open_val: float | None, price: float) -> float:
    """open 缺值 fallback：NaN/None → 同 bar close（設計 §4.2 適用本節所有出場價與 §5 進場價）。"""
    if open_val is None or (isinstance(open_val, float) and pd.isna(open_val)):
        return price
    return open_val


# ── 漲跌停鎖死判定（純函式，設計 §3）─────────────────────────────────

def _volume_thin(volume: float | None, vol_ma20: float | None) -> bool:
    """量能枯竭判定（D3/U3 共用）。vol_ma20 為 None（樣本不足/NaN/0）時視為成立
    （寧可誤判鎖死，方向保守，見 §3.1）；volume 為 NaN 視為 0。"""
    if vol_ma20 is None or (isinstance(vol_ma20, float) and pd.isna(vol_ma20)) or vol_ma20 <= 0:
        return True
    vol = volume if (volume is not None and not (isinstance(volume, float) and pd.isna(volume))) else 0.0
    return vol <= LOCK_VOLUME_RATIO * vol_ma20


def is_limit_down_locked(bar: dict, prev_close: float | None, vol_ma20: float | None) -> bool:
    """出場側跌停鎖死判定（設計 §3.1）：D1（寬鬆 sanity）∧ D2（收盤=最低）∧ D3（量能枯竭）。

    bar 需含 "close"/"low"/"volume"（"high" 不參與本判定）。prev_close 缺失或
    非正值時無法計算比值帶，回傳 False（退化為既有「未鎖死」路徑，不新增風險）。
    """
    close = bar.get("close")
    low = bar.get("low")
    volume = bar.get("volume")
    if prev_close is None or prev_close <= 0 or close is None or low is None:
        return False

    ratio = close / prev_close
    d1 = 0.5 <= ratio <= 0.906
    d2 = (close - low) <= prev_close * 0.002
    d3 = _volume_thin(volume, vol_ma20)
    return d1 and d2 and d3


def is_one_price_limit_up(bar: dict, prev_close: float | None, vol_ma20: float | None) -> bool:
    """進場側一字漲停 gate（設計 §3.2）：U1（全日單一價位）∧ U2（方向向上）∧ U3（量能枯竭）。

    bar 需含 "high"/"low"/"close"/"volume"。prev_close 缺失或非正值時回傳 False。
    """
    high = bar.get("high")
    low = bar.get("low")
    close = bar.get("close")
    volume = bar.get("volume")
    if prev_close is None or prev_close <= 0 or high is None or low is None or close is None:
        return False

    u1 = (high - low) <= prev_close * 0.002
    u2 = close > prev_close
    u3 = _volume_thin(volume, vol_ma20)
    return u1 and u2 and u3


# ── 資料下載 ─────────────────────────────────────────────────────────

def _fetch_latest(symbols: list[str]) -> dict[str, dict]:
    """批次下載最新收盤價、盤中高低點、EMA，以及 Phase 3 鎖死判定所需的
    open/prev_close/volume/vol_ma20/bar_date。High/Low 與 Close 同列對齊，
    NaN 時 fallback 為 close。"""
    if not symbols:
        return {}
    try:
        raw = yf.download(
            tickers=symbols,
            period="60d",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as e:
        print(f"[tracker] 下載追蹤股票數據失敗：{e}")
        return {}

    def _get_df(sym: str) -> pd.DataFrame:
        try:
            df = raw[sym] if len(symbols) > 1 else raw
            return df.dropna(how="all")
        except Exception:
            return pd.DataFrame()

    result: dict[str, dict] = {}
    for sym in symbols:
        df = _get_df(sym)
        if df.empty:
            continue
        close = df["Close"].dropna()
        if close.empty:
            continue
        price_date = close.index[-1]
        price = float(close.iloc[-1])
        ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1]) if len(close) >= 20 else None
        ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1]) if len(close) >= 50 else None

        # 日內高低點：必須與 price 同一列（price_date）讀取，避免 Close 缺值時
        # dropna() 讓 price 落在前一列、但 High/Low 卻取自最新殘缺列而日期錯位；
        # NaN fallback → close，避免停損免疫 Bug
        high_raw = df["High"].loc[price_date] if "High" in df.columns else float("nan")
        low_raw  = df["Low"].loc[price_date]  if "Low"  in df.columns else float("nan")
        today_high = float(high_raw) if pd.notna(high_raw) else price
        today_low  = float(low_raw)  if pd.notna(low_raw)  else price

        # ── Phase 3 新增欄位（設計 §6）──
        open_raw = df["Open"].loc[price_date] if "Open" in df.columns else float("nan")
        open_val = float(open_raw) if pd.notna(open_raw) else None

        prior_close = close[close.index < price_date]
        prev_close = float(prior_close.iloc[-1]) if not prior_close.empty else None

        volume_series = df["Volume"] if "Volume" in df.columns else pd.Series(dtype=float)
        volume_raw = volume_series.loc[price_date] if price_date in volume_series.index else float("nan")
        volume_val = float(volume_raw) if pd.notna(volume_raw) else 0.0

        # vol_ma20：不含當日的前 20 個交易日均量；不足 10 根有效值 ⇒ 樣本不足（None）
        prior_volume = volume_series[volume_series.index < price_date].dropna().tail(20)
        if len(prior_volume) < 10:
            vol_ma20 = None
        else:
            v = float(prior_volume.mean())
            vol_ma20 = v if v > 0 else None

        result[sym] = {
            "price":        round(price, 2),
            "today_high":   round(today_high, 2),
            "today_low":    round(today_low, 2),
            "open":         round(open_val, 2) if open_val is not None else None,
            "prev_close":   round(prev_close, 2) if prev_close is not None else None,
            "volume":       volume_val,
            "vol_ma20":     vol_ma20,
            "bar_date":     price_date.date().isoformat(),
            "ema20":        round(ema20, 2) if ema20 else None,
            "ema50":        round(ema50, 2) if ema50 else None,
            "close_series": close,
        }

    return result


def _calc_split_factor(signal_date: str, signal_date_close: float,
                       close_series: pd.Series) -> float:
    """
    從 yfinance auto_adjust 的歷史數據中查找信號日的調整後收盤價，
    計算拆股平移因子。無拆股時回傳 1.0。
    """
    if not signal_date or not signal_date_close:
        return 1.0
    try:
        idx = pd.to_datetime(signal_date)
        valid = close_series[close_series.index.normalize() <= idx]
        if valid.empty:
            return 1.0
        adjusted_hist = float(valid.iloc[-1])
        return adjusted_hist / signal_date_close
    except Exception:
        return 1.0


def _eval_status(
    entry: dict,
    price: float,
    ema20: float | None,
    ema50: float | None = None,
    today_low: float | None = None,
) -> tuple[str, str | None]:
    """
    評估訊號狀態：股價是否已落入買入區間，或訊號是否失效。
    回傳 (new_status, invalid_reason)。
    已失效者直接回傳原因，不再重新判斷。

    盤中限價單模擬進場：使用者實際下單方式是在買入區間上緣（buy_zone_upper）
    掛限價單，只要 today_low <= buy_zone_upper 即視為當日觸價成交，優先於下方
    所有以收盤價 price 為準的判定（含追高失效）。此檢查嚴格位於 invalid/active
    短路之後、其餘判定之前，確保既有 invalid 條目不會被追溯復活。today_low 為
    None（呼叫端未提供）或今日未觸價（today_low > buy_zone_upper）時，完全退化
    為下方原本以收盤價為準的判定。

    active 部位不在此函式判定失效或到期：生命週期完全交給 _check_settlement()
    的結算。此函式若對 active 部位另外翻 invalid，會使該部位繞過結算、不寫入
    performance_history.json 便被 _is_expired() 無聲移除。
    """
    if entry.get("status") == "invalid":
        return "invalid", entry.get("invalid_reason")
    if entry.get("status") == "active":
        return "active", None

    # ── 盤中限價單模擬進場：today_low <= upper 即視為觸價成交 ──
    if today_low is not None and today_low <= entry["buy_zone_upper"]:
        return "active", None

    lower = entry.get("buy_zone_lower", 0.0)
    upper = entry["buy_zone_upper"]
    strategy = entry.get("strategy", "")
    stop_loss_price = _parse_stop_loss(entry.get("stop_loss", "-"))

    # ── 失效條件：依策略差異化 ──
    if strategy == "反轉策略":
        if stop_loss_price is not None and price < stop_loss_price:
            return "invalid", f"跌破止損價 ${stop_loss_price:.2f}，反轉訊號失效"
    else:
        if ema20 is not None and price < ema20:
            return "invalid", "趨勢轉弱，訊號失效"

    # ── 追高失效（僅 watch 階段可達，active 已於上方短路）──
    if price > upper * 1.08:
        return "invalid", "已追高，錯過買點"

    # ── 狀態機判定 ──
    if price > upper * 1.01:
        return "watch", None       # 高於買入區間，等回落
    if price >= lower:
        if stop_loss_price is not None and price <= stop_loss_price:
            return "invalid", f"開盤跳空跌破止損價 ${stop_loss_price:.2f}，拒絕進場"
        return "active", None      # 在買入區間內且高於止損，視為進場
    # price < lower：跌穿買入區下限
    if stop_loss_price is not None and price < stop_loss_price:
        return "invalid", f"跌破止損價 ${stop_loss_price:.2f}，錯過買點"
    return "watch", None           # 跌穿下限但未到止損，繼續觀察


def _check_settlement(
    entry: dict,
    price: float,
    today_high: float | None = None,
    today_low: float | None = None,
    open_: float | None = None,
    prev_close: float | None = None,
    volume: float | None = None,
    vol_ma20: float | None = None,
) -> tuple | None:
    """
    判斷 active 部位是否觸發結算，或（Phase 3 新增）是否應順延至跌停解除。
    純決策函式：只在主迴圈已確認本輪為新鮮 bar 後才會被呼叫（§4.5），
    stale 計數與新鮮度判定完全在主迴圈處理，不屬於本函式職責。

    回傳四種轉移指令之一（設計 §4.1）：
      ("defer", {"pending_vol_baseline": <float|None>})   # 轉入 pending，本日不結算
      ("hold_pending", None)                              # 維持 pending
      ("exit", exit_reason, exit_price, exit_note)         # 結算
      None                                                 # 無事發生

    優先順序（§4.4）：
      1. pending_exit=True：判新 bar 鎖死與否，不看停利/到期
      2. 觸停損：依 §4.2 三分支（defer / 鎖死∧¬H1 close 出場 / 未鎖死 min(stop,open)）
      3. 停利：不受漲停鎖死影響，照常成交（黑天鵝日先過第 2 步）
      4. 移動停利
      5. 時間到期
    """
    if entry.get("status") != "active":
        return None

    target      = _parse_target(entry.get("target", "-"))
    stop_loss   = (entry.get("effective_stop_loss")
                   or _parse_stop_loss(entry.get("stop_loss", "-")))
    hold_limit  = _parse_hold_period(entry.get("hold_period", "-"))
    active_days = entry.get("active_days", 0)
    eff_open    = _open_or_close(open_, price)
    bar = {"close": price, "low": today_low, "high": today_high, "volume": volume}

    # 1. pending_exit：判新 bar 鎖死與否（用轉入當日凍結的量能基準）
    if entry.get("pending_exit"):
        baseline = entry.get("pending_vol_baseline")
        if is_limit_down_locked(bar, prev_close, baseline):
            return ("hold_pending", None)
        return ("exit", EXIT_LOSS, eff_open, "limit_down_deferred")

    # 2. 觸停損
    if today_low is not None and stop_loss is not None and today_low <= stop_loss:
        locked = is_limit_down_locked(bar, prev_close, vol_ma20)
        h1 = today_high is not None and today_high < stop_loss
        if locked and h1:
            return ("defer", {"pending_vol_baseline": vol_ma20})
        if locked and not h1:
            return ("exit", EXIT_LOSS, price, "limit_down_thin_fill")
        exit_price = min(stop_loss, eff_open)
        return ("exit", EXIT_LOSS, exit_price, None)

    # 3. 停利（不受漲停鎖死影響，P3）
    if today_high is not None and target is not None and today_high >= target:
        return ("exit", EXIT_PROFIT, target, None)

    # 4. 移動停利（收盤觸發；精確排除反轉策略）
    strategy = entry.get("strategy") or entry.get("assigned_strategy") or ""
    if strategy != "反轉策略":
        entry_price  = entry.get("active_entry_price") or 0
        prev_highest = entry.get("highest_close_since_active") or entry_price
        if entry_price > 0 and prev_highest > entry_price:
            max_gain_pct = (prev_highest - entry_price) / entry_price
            retrace_pct  = (prev_highest - price) / prev_highest
            if max_gain_pct >= TRAILING_ACTIVATION_PCT and retrace_pct >= TRAILING_RETRACE_PCT:
                return ("exit", EXIT_TRAILING, price, None)

    # 5. 持倉天數到期
    if active_days >= hold_limit:
        return ("exit", EXIT_EXPIRED, price, None)

    return None


def _archive_to_performance_history(
    entry: dict,
    exit_reason: str,
    exit_price: float,
    exit_date: str,
    exit_note: str | None = None,
    locked_days: int = 0,
    exit_deferred: bool = False,
) -> None:
    """將結算部位寫入 data/performance_history.json（原子寫入）。"""
    entry_price = entry.get("active_entry_price") or entry.get("buy_zone_lower", 0)
    if entry_price and entry_price > 0:
        return_pct = round((exit_price - entry_price) / entry_price * 100, 2)
    else:
        return_pct = None

    active_start = entry.get("active_start_date") or entry.get("date_added", "")
    holding_days = entry.get("active_days") or (
        _count_trading_days(active_start, exit_date) if active_start and exit_date else 0
    )

    record = {
        "meta_data": {
            "ticker":       entry["symbol"],
            "company_name": entry.get("name", ""),
            "sector":       entry.get("sector", ""),
        },
        "signal_details": {
            "signal_date":        entry.get("date_added", ""),
            "entry_regime":       entry.get("entry_regime", ""),
            "market_breadth_pct": entry.get("market_breadth_pct"),
            "vix_value":          entry.get("vix_value"),
            "l2_score":           entry.get("l2_score"),
            "assigned_strategy":  entry.get("strategy", ""),
            "ai_confidence":      entry.get("ai_confidence"),
            "ai_strategy_reason": entry.get("ai_strategy_reason", ""),
        },
        "execution_plan": {
            "buy_zone_lower":    entry.get("buy_zone_lower"),
            "buy_zone_upper":    entry.get("buy_zone_upper"),
            "planned_target":    entry.get("target", "-"),
            "planned_stop_loss": (
                f"${entry['planned_stop_loss']:.2f}"
                if entry.get("planned_stop_loss")
                else entry.get("stop_loss", "-")
            ),
        },
        "actual_outcome": {
            "triggered_date":     entry.get("active_start_date", ""),
            "actual_entry_price": entry.get("active_entry_price"),
            "exit_date":          exit_date,
            "actual_exit_price":  round(exit_price, 2),
            "exit_reason":        exit_reason,
            "holding_days":       holding_days,
            "exit_note":          exit_note,
            "locked_days":        locked_days,
            "exit_deferred":      exit_deferred,
        },
        "performance_metrics": {
            "return_pct": return_pct,
            # 純數學判定，與出場原因解耦：CLOSED_TRAILING_STOP 正回報同樣計 Win
            "is_win":     return_pct > 0 if return_pct is not None else None,
        },
    }

    if _PERF_PATH.exists():
        try:
            with open(_PERF_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {"history_records": []}
    else:
        data = {"history_records": []}

    data["history_records"].append(record)

    # 原子寫入：先寫暫存檔再 rename，防止寫入中途崩潰導致 JSON 損壞
    tmp_path = _PERF_PATH.with_suffix(".tmp")
    _DATA_DIR.mkdir(exist_ok=True)
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp_path.replace(_PERF_PATH)

    sign = f"{return_pct:+.2f}%" if return_pct is not None else "N/A"
    print(f"[tracker] {entry['symbol']} 結算歸檔（{exit_reason}，回報 {sign}）")


def _apply_exit(
    entry: dict,
    exit_reason: str,
    exit_price: float,
    exit_note: str | None,
    today: str,
    settled_entries: list[dict],
) -> None:
    """套用 exit 指令：§4.4 正常結算與 §4.5 強制結算共用同一段程式碼
    （設計 §4.5「接線」段），完整跑完既有的結算下游，不重寫第二套歸檔邏輯。"""
    _archive_to_performance_history(
        entry, exit_reason, exit_price, today,
        exit_note=exit_note,
        locked_days=entry.get("locked_days", 0),
        exit_deferred=entry.get("locked_days", 0) > 0,
    )
    entry["_settled"]     = True
    entry["_exit_reason"] = exit_reason
    entry["_exit_price"]  = exit_price
    entry["_exit_note"]   = exit_note  # 供 publisher.py 區分漲跌停鎖死相關出場與一般出場
    settled_entries.append(entry)


def _apply_risk_controls(
    adj: dict, price: float, split_factor: float, original_entry: dict
) -> None:
    """
    保本鎖定與最高收盤更新。
    adj 中的欄位為 split-scaled 調整後標尺，用於本輪結算比對。
    original_entry 以原生未拆股標尺持久化至 watchlist.json，避免逆向除法累積誤差。
    """
    if adj.get("status") != "active":
        return

    entry_price    = adj.get("active_entry_price") or 0
    target         = _parse_target(adj.get("target", "-"))
    effective_sl   = adj.get("effective_stop_loss")
    buy_zone_upper = adj.get("buy_zone_upper", 0)
    prev_highest   = adj.get("highest_close_since_active") or entry_price

    if entry_price <= 0:
        return

    # 向後相容：存量 active 持倉首次遇到新版 code 時，一次性初始化風控欄位
    if effective_sl is None:
        fallback_sl = (adj.get("planned_stop_loss")
                       or _parse_stop_loss(adj.get("stop_loss", "-")))
        adj["planned_stop_loss"]              = fallback_sl
        adj["effective_stop_loss"]            = fallback_sl
        adj["is_breakeven_locked"]            = False
        original_entry["planned_stop_loss"]   = fallback_sl
        original_entry["effective_stop_loss"] = fallback_sl
        original_entry.setdefault("is_breakeven_locked", False)
        effective_sl = fallback_sl

    # ── 保本鎖定（明示旗標，非浮點差判定）──
    if (not adj.get("is_breakeven_locked", False)
            and target is not None
            and effective_sl is not None
            and buy_zone_upper > (effective_sl or 0)):
        breakeven_threshold = entry_price + (target - entry_price) * BREAKEVEN_PROFIT_THRESHOLD
        if price >= breakeven_threshold:
            adj["effective_stop_loss"] = buy_zone_upper
            adj["is_breakeven_locked"] = True
            raw_upper = original_entry.get("buy_zone_upper") or (buy_zone_upper / split_factor)
            original_entry["effective_stop_loss"] = raw_upper
            original_entry["is_breakeven_locked"] = True
            print(f"[tracker] {adj.get('symbol', '')} 保本鎖定：止損上移至 ${raw_upper:.2f}")

    # ── 最高收盤更新（只在原生標尺創新高時才寫回 DB）──
    today_price_raw = price / split_factor
    stored_highest  = original_entry.get("highest_close_since_active") or 0
    if today_price_raw > stored_highest:
        original_entry["highest_close_since_active"] = today_price_raw
        adj["highest_close_since_active"]            = price
    # 未創新高：original_entry 保持唯讀，不累積浮點誤差


def _days(entry: dict) -> int:
    """回傳已追蹤天數（唯一日期數量）。供 is_rerun 防重複執行使用。"""
    return len(entry.get("tracked_dates", []))


def _slot_priority_key(entry: dict) -> tuple:
    """
    掛單名單優先序：ai_confidence 高者優先取得持倉名額，同分比 l2_score，
    再同分依 symbol 字母序保證確定性。缺值以 0 處理（排最後）。
    """
    return (
        -(entry.get("ai_confidence") or 0),
        -(entry.get("l2_score") or 0),
        entry.get("symbol", ""),
    )


def compute_order_plan(watchlist: list[dict]) -> dict:
    """
    計算事前掛單名單：使用者依報告隔日只會掛「優先序前 free_slots 名」的限價單，
    此名單即 watch→active 的進場資格。pending_exit 部位 status 仍為 "active"，
    天然計入 active_count 佔槽（設計 §4.1）。
    """
    active_count = sum(1 for e in watchlist if e.get("status") == "active")
    free_slots = max(0, MAX_ACTIVE_POSITIONS - active_count)
    roster = sorted(
        (e for e in watchlist if e.get("status") == "watch"),
        key=_slot_priority_key,
    )
    eligible = {e["symbol"] for e in roster[:free_slots]}
    return {"free_slots": free_slots, "roster": roster, "eligible": eligible}


def _max_watch_days(entry: dict) -> int:
    """依策略與訊號當下大盤環境回傳 watch/invalid 天數上限。"""
    strategy = entry.get("strategy", "")
    regime   = entry.get("entry_regime", "")
    vix      = entry.get("vix_value")

    if strategy == "突破策略" and regime == "CONSOLIDATION_VOLATILE":
        return 3  # 高波動整理市假突破風險升高，縮短觀察期

    if strategy == "反轉策略" and regime == "PANIC_REVERSAL" and vix is not None and vix > 35:
        return 5  # VIX 暴噴級尖底，V 型反彈應快速兌現，遲遲不進場視為真黑天鵝

    return _WATCH_DAYS_BY_STRATEGY.get(strategy, _DEFAULT_WATCH_DAYS)


def _is_expired(entry: dict) -> bool:
    """
    判斷是否已到期應移除。
    - watch / invalid：超過策略對應 watch 上限個追蹤日即到期
    - active（含 pending_exit，status 仍為 "active"）：由 _check_settlement() 接管，此處永不到期
    """
    status = entry.get("status", "watch")
    if status == "active":
        return False   # active 部位由結算邏輯控制生命週期
    return _days(entry) >= _max_watch_days(entry)


# ── 主函式 ──────────────────────────────────────────────────────────

def run_tracker(
    new_ranked: list[dict],
    market_context: dict | None = None,
    market_date: str | None = None,
) -> tuple[list[dict], dict]:
    """
    執行訊號追蹤流程。
    回傳 (updated_watchlist, categories)。

    執行順序：D（下載現有）→ E（評估現有，含 Phase 3 鎖死順延）→ B/C（處理新訊號）。
    新訊號在當輪不被評估（1-day lag），下一個交易日才進入狀態機。

    categories 結構：
      active:   已落入買入區間的追蹤中股票（含 pending_exit 排隊中股票）
      watch:    等待回落的追蹤中股票
      invalid:  訊號失效但未到期的股票
      expired:  今日到期移除的股票（快照）
      settled:  今日觸發結算並歸檔的股票（快照）
      new:      本次新加入的股票（含完整 AI 資料）
      reset:    本次重新入選並重置的股票（含完整 AI 資料）
    """
    today = market_date or date.today().isoformat()
    watchlist = load_watchlist()
    mc = market_context or {}

    # 相容舊格式（days_tracked int → tracked_dates list）
    for entry in watchlist:
        if "tracked_dates" not in entry:
            entry["tracked_dates"] = []

    # 同一天重跑時，清除今天才新增的股票（讓新結果完整取代）
    # 跨日追蹤中的舊股票（date_added != today）不受影響
    is_rerun = any(today in e.get("tracked_dates", []) for e in watchlist)
    if is_rerun:
        watchlist = [e for e in watchlist if e.get("date_added") != today]
        print(f"[tracker] 今日重複執行，已清除今日新增的股票，重新以新結果取代")

    existing = {e["symbol"]: e for e in watchlist}

    # ── D. 批次下載現有持倉最新價格（High/Low/Close/EMA + Phase 3 欄位）────
    existing_symbols = list(existing.keys())
    latest = _fetch_latest(existing_symbols)
    print(f"[tracker] 追蹤清單：{len(existing_symbols)} 支，成功取得 {len(latest)} 支最新數據")

    # ── E. 評估現有持倉狀態、更新計數器、執行結算 ───────────────────
    settled_entries: list[dict] = []

    # 事前掛單名單：E 開頭一次性計算，即前晚報告「明日掛單計畫」的名單（確定性重算）。
    # 名單於當日內固定——當日結算不退還名額（1-day lag）。
    eligible = compute_order_plan(watchlist)["eligible"]

    for entry in watchlist:
        sym = entry["symbol"]

        # 滿倉旗標僅反映今日：置於 continue 之前重置，確保下載失敗日不殘留昨日的 True
        entry["slot_blocked_today"] = False

        # 同日重跑判定：tracked_dates 是否已含今日。watch_days/active_days/
        # locked_days/pending_stale_runs 的遞增必須依此去重，否則同一天內多次
        # 執行（手動重跑並確認繼續）會讓計數器被重複累加。
        already_tracked_today = today in entry["tracked_dates"]
        if not already_tracked_today:
            entry["tracked_dates"].append(today)

        # ── Phase 3 §4.5 新鮮度守衛（單一守衛，須在 sym not in latest continue 之前）──
        is_fresh = (sym in latest) and (latest[sym].get("bar_date") == today)

        if entry.get("pending_exit") and not is_fresh:
            if not already_tracked_today:
                entry["pending_stale_runs"] = entry.get("pending_stale_runs", 0) + 1
                # pending 部位 status 仍是 "active"，stale 輪次只是跳過結算判定，
                # 不是跳過持有計時（沿用母本既有 active_days 計數規則）
                entry["active_days"] = entry.get("active_days", 0) + 1
            if entry.get("pending_stale_runs", 0) >= PENDING_STALE_LIMIT:
                exit_price = entry.get("current_price")
                if exit_price is None:
                    exit_price = entry.get("effective_stop_loss") or _parse_stop_loss(entry.get("stop_loss", "-"))
                _apply_exit(entry, EXIT_LOSS, exit_price, "force_settled_after_stale_limit", today, settled_entries)
            # 未達門檻：hold_pending_stale，不呼叫 _check_settlement，
            # current_price 維持上一輪的值不覆寫
            continue

        if sym not in latest:
            entry.setdefault("current_price", None)
            continue

        # 走到這裡：is_fresh=True，或 entry 非 pending_exit 部位（維持母本既有行為）
        if entry.get("pending_exit"):
            entry["pending_stale_runs"] = 0

        price        = latest[sym]["price"]
        ema20        = latest[sym]["ema20"]
        ema50        = latest[sym].get("ema50")
        close_series = latest[sym].get("close_series")
        today_high   = latest[sym].get("today_high")
        today_low    = latest[sym].get("today_low")
        open_        = latest[sym].get("open")
        prev_close   = latest[sym].get("prev_close")
        volume       = latest[sym].get("volume")
        vol_ma20     = latest[sym].get("vol_ma20")

        # ── Phase 3 §5.1：一字漲停 gate（僅在 status=="watch" 時套用覆寫，
        # 非 watch 態一律跳過 gate、走 _eval_status 正常路徑；其內部短路已正確
        # 處理 active/invalid，覆寫模式不可介入已持有部位，見設計 §5.1）──
        gate_bar = {"close": price, "low": today_low, "high": today_high, "volume": volume}
        gate_override = (
            entry.get("status") == "watch"
            and is_one_price_limit_up(gate_bar, prev_close, vol_ma20)
        )

        # 拆股免疫：以信號日的 auto_adjust 歷史價計算平移因子
        signal_close = entry.get("signal_date_close")
        signal_date  = entry["tracked_dates"][0] if entry.get("tracked_dates") else ""
        split_factor = 1.0
        if signal_close and close_series is not None:
            split_factor = _calc_split_factor(signal_date, signal_close, close_series)
        if abs(split_factor - 1.0) > 0.01:
            print(f"[tracker] {sym} 偵測到拆股，平移因子={split_factor:.4f}")
            adj = dict(entry)
            adj["buy_zone_lower"] = entry.get("buy_zone_lower", 0.0) * split_factor
            adj["buy_zone_upper"] = entry["buy_zone_upper"] * split_factor
            sl = _parse_stop_loss(entry.get("stop_loss", "-"))
            if sl:
                adj["stop_loss"] = f"${sl * split_factor:.2f}"
            tgt = _parse_target(entry.get("target", "-"))
            if tgt:
                adj["target"] = f"${tgt * split_factor:.2f}"
            planned_sl = entry.get("planned_stop_loss") or sl
            eff_sl     = entry.get("effective_stop_loss") or planned_sl
            adj["planned_stop_loss"]  = planned_sl * split_factor if planned_sl else None
            adj["effective_stop_loss"] = eff_sl    * split_factor if eff_sl    else None
            adj["is_breakeven_locked"] = entry.get("is_breakeven_locked", False)
            adj["active_entry_price"]          = (entry.get("active_entry_price") or 0) * split_factor
            highest = entry.get("highest_close_since_active") or entry.get("active_entry_price") or 0
            adj["highest_close_since_active"]  = highest * split_factor
            if gate_override:
                new_status, reason = "watch", None
            else:
                new_status, reason = _eval_status(adj, price, ema20, ema50, today_low=today_low)
            settlement_entry = adj   # 結算也使用縮放後的 adj
        else:
            if gate_override:
                new_status, reason = "watch", None
            else:
                new_status, reason = _eval_status(entry, price, ema20, ema50, today_low=today_low)
            settlement_entry = entry

        prev_status = entry.get("status", "watch")

        # 名單制閘門：watch→active 僅限事前掛單名單內的條目（使用者只掛了名單內的
        # 限價單）。名單外觸價者以收盤價重新判定（today_low=None，純函式重跑）：
        # 失效者直接清除，其餘強制維持 watch 並標記，次日以新名單重新競爭。
        if new_status == "active" and prev_status == "watch" and sym not in eligible:
            closing_status, closing_reason = _eval_status(
                settlement_entry, price, ema20, ema50, today_low=None
            )
            if closing_status == "invalid":
                new_status, reason = "invalid", closing_reason
                print(f"[tracker] {sym} 觸價但未在掛單名單，收盤價判定失效：{closing_reason}")
            else:
                new_status, reason = "watch", None
                entry["slot_blocked_today"] = True
                print(f"[tracker] {sym} 觸價但未在掛單名單（名額 {MAX_ACTIVE_POSITIONS} 支已滿或優先序不足），今日不進場")

        entry["status"]         = new_status
        entry["invalid_reason"] = reason
        entry["current_price"]  = price

        if entry.get("signal_date_close") is None:
            entry["signal_date_close"] = price

        # 首次進入 active：記錄代理進場價、日期，並初始化風控欄位
        if new_status == "active" and prev_status == "watch":
            # Phase 3 §5.2：跌停穿越買入區時，成交價由 buy_zone_upper 改為
            # min(buy_zone_upper, open)——正常回落仍以 upper 成交，跳空開低/
            # 一字跌停則以開盤市價成交，如實入帳接刀成本。
            eff_open_fill = _open_or_close(open_, price)
            entry_fill_price = min(settlement_entry["buy_zone_upper"], eff_open_fill)
            if entry.get("active_entry_price") is None:
                entry["active_entry_price"] = entry_fill_price
                entry["active_start_date"]  = today
            if entry.get("planned_stop_loss") is None:
                planned_val = _parse_stop_loss(entry.get("stop_loss", "-"))
                entry["planned_stop_loss"]   = planned_val
                entry["effective_stop_loss"] = planned_val
            entry.setdefault("is_breakeven_locked", False)
            if entry.get("highest_close_since_active") is None:
                entry["highest_close_since_active"] = price

            if settlement_entry is not entry:
                settlement_entry["active_entry_price"]         = entry_fill_price
                settlement_entry["planned_stop_loss"]          = (entry["planned_stop_loss"] or 0) * split_factor
                settlement_entry["effective_stop_loss"]        = settlement_entry["planned_stop_loss"]
                settlement_entry["is_breakeven_locked"]        = False
                settlement_entry["highest_close_since_active"] = price
            else:
                settlement_entry["active_entry_price"]         = entry_fill_price
                settlement_entry["planned_stop_loss"]          = entry["planned_stop_loss"]
                settlement_entry["effective_stop_loss"]        = entry["effective_stop_loss"]
                settlement_entry["is_breakeven_locked"]        = False
                settlement_entry["highest_close_since_active"] = price

        # 計數器遞增（直接寫入 entry，確保被 JSON 序列化）
        # 同日重跑（already_tracked_today=True）不重複遞增
        if not already_tracked_today:
            if new_status == "watch":
                entry["watch_days"] = entry.get("watch_days", 0) + 1
            elif new_status == "active":
                entry["active_days"] = entry.get("active_days", 0) + 1

        # 風控更新：保本鎖定 + 最高收盤追蹤；僅持續 active 狀態執行，
        # pending_exit 部位跳過（設計 §4.1／§6：FORCE_EXPIRED 與
        # _apply_risk_controls 加 pending_exit 旗標跳過）
        if prev_status == "active" and new_status == "active" and not entry.get("pending_exit"):
            _apply_risk_controls(settlement_entry, price, split_factor, entry)

        # 結算檢查（Phase 3：defer/hold_pending/exit 三種指令）
        settlement = _check_settlement(
            settlement_entry, price, today_high, today_low,
            open_=open_, prev_close=prev_close, volume=volume, vol_ma20=vol_ma20,
        )
        if settlement is not None:
            kind = settlement[0]
            if kind == "defer":
                payload = settlement[1]
                entry["pending_exit"] = True
                entry["pending_vol_baseline"] = payload["pending_vol_baseline"]
                entry.setdefault("locked_days", 0)
                entry.setdefault("pending_stale_runs", 0)
            elif kind == "hold_pending":
                if not already_tracked_today:
                    entry["locked_days"] = entry.get("locked_days", 0) + 1
            elif kind == "exit":
                _, exit_reason, exit_price, exit_note = settlement
                _apply_exit(entry, exit_reason, exit_price, exit_note, today, settled_entries)

    # ── B/C（後移）. 處理今日 L3 新訊號（雙軌分流）──────
    # 重建 existing，反映 E 後的最新狀態（含 status 變化）
    existing = {e["symbol"]: e for e in watchlist}
    reset_symbols: set[str] = set()
    new_entries: list[dict] = []
    reset_entries: list[dict] = []

    for stock in new_ranked:
        sym = stock["symbol"]
        if stock.get("is_fallback"):
            print(f"[tracker] {sym} 為 L2 分數 fallback 結果（AI 未產生有效判斷），不納入追蹤")
            continue
        confidence = stock.get("confidence") or 0
        if confidence < MIN_AI_CONFIDENCE:
            print(f"[tracker] {sym} AI 信心分數 {confidence} < {MIN_AI_CONFIDENCE}，跳過")
            continue
        parsed = _parse_buy_zone(stock.get("buy_zone", "-"))
        if parsed is None:
            continue

        lower, upper = parsed
        base: dict = {
            "buy_zone":           stock["buy_zone"],
            "buy_zone_lower":     lower,
            "buy_zone_upper":     upper,
            "target":             stock.get("target", "-"),
            "stop_loss":          stock.get("stop_loss", "-"),
            "hold_period":        stock.get("hold_period", "-"),
            "strategy":           stock.get("strategy", "-"),
            "tracked_dates":      [today],
            "status":             "watch",
            "invalid_reason":     None,
            "slot_blocked_today": False,
            # ── 計時器（持久化至 JSON）──
            "watch_days":         0,
            "active_days":        0,
            "signal_date_close":  stock.get("price"),
            # ── 進場追蹤 ──
            "active_entry_price": None,
            "active_start_date":  None,
            # ── 日期錨定 ──
            "date_added":         today,
            # ── 信號時刻大盤背景（供績效分析） ──
            "entry_regime":       mc.get("regime", ""),
            "market_breadth_pct": mc.get("market_breadth_pct"),
            "vix_value":          mc.get("vix", {}).get("value"),
            # ── AI 精選資訊 ──
            "l2_score":           stock.get("total_score"),
            "ai_confidence":      stock.get("confidence"),
            "ai_strategy_reason": stock.get("strategy_reason", ""),
        }
        if sym in existing:
            if existing[sym].get("status") == "active":
                # active 持倉再入選（含 pending_exit）：訊號免疫，跳過重置
                print(f"[tracker] {sym} 已持倉（active），跳過重置，沿用原交易計劃")
            else:
                # watch / invalid：訊號覆寫展期，重置觀察期與 AI 參數
                existing[sym].update(base)
                reset_symbols.add(sym)
                reset_entries.append(stock)
        else:
            # 全新個股：加入 watchlist，本輪不評估（1-day lag 天然實現）
            watchlist.append({
                "symbol": sym,
                "name":   stock.get("name", sym),
                "sector": stock.get("sector", "Unknown"),
                **base,
            })
            new_entries.append(stock)

    # ── F. 分類（移除前快照）────────────────────────────────────────
    settled_symbols = {e["symbol"] for e in settled_entries}
    expired = [e for e in watchlist if _is_expired(e) and e["symbol"] not in settled_symbols]
    active = [
        e for e in watchlist
        if e["status"] == "active"
        and e["symbol"] not in reset_symbols
        and e["symbol"] not in settled_symbols
        and not _is_expired(e)
    ]
    watch = [
        e for e in watchlist
        if e["status"] == "watch"
        and e["symbol"] not in reset_symbols
        and e["symbol"] not in settled_symbols
        and not _is_expired(e)
    ]
    invalid = [
        e for e in watchlist
        if e["status"] == "invalid"
        and e["symbol"] not in reset_symbols
        and e["symbol"] not in settled_symbols
        and not _is_expired(e)
    ]

    categories = {
        "active":   active,
        "watch":    watch,
        "invalid":  invalid,
        "expired":  expired,
        "settled":  settled_entries,
        "new":      new_entries,
        "reset":    reset_entries,
    }

    # ── G. 移除已到期與已結算 ────────────────────────────────────────
    watchlist = [
        e for e in watchlist
        if not _is_expired(e) and not e.get("_settled")
    ]

    # 明日掛單名單：對移除後的最終 watchlist 計算，供 publisher 渲染「明日掛單計畫」
    categories["order_plan"] = compute_order_plan(watchlist)

    # ── H. 儲存 ──────────────────────────────────────────────────────
    save_watchlist(watchlist)
    print(f"[tracker] watchlist 更新完成，保留 {len(watchlist)} 筆"
          f"（結算歸檔 {len(settled_entries)} 筆）")

    return watchlist, categories

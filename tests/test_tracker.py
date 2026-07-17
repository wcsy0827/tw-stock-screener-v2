"""tracker.py 單元測試，聚焦 Phase 3 漲跌停止損模擬機制
（見 docs/phase3_limit_lock_design.md v10）：
- is_limit_down_locked / is_one_price_limit_up 純函式（鎖死/盤中打開/跳空/
  除權息缺股利/資料異常/樣本不足/一字漲停/一字跌停 fixture，設計 §6）
- _check_settlement 的 defer/hold_pending/exit 三種指令（設計 §4.2/§4.4）
- run_tracker 主迴圈的新鮮度守衛與強制結算（設計 §4.5）
- §5.1 一字漲停 gate 的 status=="watch" 前提
- §5.2 跌停穿越買入區的進場價
- Q1：pending 期間拆股時 pending_vol_baseline 的標尺不受 split_factor 影響
"""

import json

import pandas as pd
import pytest

import tracker


# ── is_limit_down_locked（出場側跌停鎖死判定，設計 §3.1）─────────────

class TestIsLimitDownLocked:
    def test_typical_locked_bar(self):
        # 收盤=最低，比值在帶內，量能枯竭 → 鎖死
        bar = {"close": 90.0, "low": 90.0, "volume": 1000}
        assert tracker.is_limit_down_locked(bar, prev_close=100.0, vol_ma20=10000.0) is True

    def test_intraday_reopened_not_thin_volume(self):
        # 已知邊界：收盤=最低、比值在帶內，但當日量能不枯竭（盤中曾打開又鎖死）
        # → 判「未鎖死」，照常結算（設計 §3.1「已知邊界」段）
        bar = {"close": 90.0, "low": 90.0, "volume": 7000}
        assert tracker.is_limit_down_locked(bar, prev_close=100.0, vol_ma20=10000.0) is False

    def test_normal_decline_not_locked(self):
        # 正常下跌日：比值在帶內但收盤不等於最低 → D2 不成立 → 未鎖死
        bar = {"close": 89.0, "low": 85.0, "volume": 500}
        assert tracker.is_limit_down_locked(bar, prev_close=100.0, vol_ma20=10000.0) is False

    def test_dividend_missing_distorts_ratio_out_of_band(self):
        # yfinance 除息缺漏導致比值超出 [0.5, 0.906] sanity 窗口，
        # 即使 D2/D3 皆成立，D1 仍拒絕 → 判「未鎖死」（設計 P4/R1 的殘餘風險）
        bar = {"close": 94.0, "low": 94.0, "volume": 200}
        assert tracker.is_limit_down_locked(bar, prev_close=100.0, vol_ma20=10000.0) is False

    def test_data_anomaly_zero_prev_close(self):
        bar = {"close": 90.0, "low": 90.0, "volume": 500}
        assert tracker.is_limit_down_locked(bar, prev_close=0.0, vol_ma20=10000.0) is False
        assert tracker.is_limit_down_locked(bar, prev_close=None, vol_ma20=10000.0) is False

    def test_data_anomaly_negative_prev_close(self):
        bar = {"close": 90.0, "low": 90.0, "volume": 500}
        assert tracker.is_limit_down_locked(bar, prev_close=-10.0, vol_ma20=10000.0) is False

    def test_insufficient_volume_sample_defaults_conservative(self):
        # vol_ma20 樣本不足（None）⇒ D3 視為成立，寧可誤判鎖死（方向保守）
        bar = {"close": 90.0, "low": 90.0, "volume": 999999}
        assert tracker.is_limit_down_locked(bar, prev_close=100.0, vol_ma20=None) is True

    def test_volume_nan_treated_as_zero(self):
        bar = {"close": 90.0, "low": 90.0, "volume": float("nan")}
        assert tracker.is_limit_down_locked(bar, prev_close=100.0, vol_ma20=10000.0) is True

    def test_one_price_limit_down_locked(self):
        # 一字跌停：high=low=close，同樣滿足 D1+D2+D3
        bar = {"close": 90.0, "low": 90.0, "volume": 100}
        assert tracker.is_limit_down_locked(bar, prev_close=100.0, vol_ma20=5000.0) is True

    def test_volume_ratio_boundary_inclusive(self):
        # volume == LOCK_VOLUME_RATIO * vol_ma20 剛好等於門檻 → 仍視為枯竭（<=）
        bar = {"close": 90.0, "low": 90.0, "volume": 6000.0}
        assert tracker.is_limit_down_locked(bar, prev_close=100.0, vol_ma20=10000.0) is True


# ── is_one_price_limit_up（進場側一字漲停 gate，設計 §3.2）───────────

class TestIsOnePriceLimitUp:
    def test_one_price_limit_up(self):
        bar = {"high": 110.0, "low": 110.0, "close": 110.0, "volume": 1000}
        assert tracker.is_one_price_limit_up(bar, prev_close=100.0, vol_ma20=10000.0) is True

    def test_normal_up_day_not_gated(self):
        # 正常上漲日：high/low 價差大，非一字型 → gate 不生效
        bar = {"high": 112.0, "low": 105.0, "close": 110.0, "volume": 1000}
        assert tracker.is_one_price_limit_up(bar, prev_close=100.0, vol_ma20=10000.0) is False

    def test_one_price_limit_down_not_gated(self):
        # 一字跌停（方向向下）：U2 不成立
        bar = {"high": 90.0, "low": 90.0, "close": 90.0, "volume": 100}
        assert tracker.is_one_price_limit_up(bar, prev_close=100.0, vol_ma20=10000.0) is False

    def test_heavy_volume_one_price_up_not_gated(self):
        # R10：主力爆量倒貨的一字漲停，U3 不成立 ⇒ gate 不生效（方向不定，接受）
        bar = {"high": 110.0, "low": 110.0, "close": 110.0, "volume": 8000}
        assert tracker.is_one_price_limit_up(bar, prev_close=100.0, vol_ma20=10000.0) is False

    def test_insufficient_volume_sample_defaults_conservative(self):
        bar = {"high": 110.0, "low": 110.0, "close": 110.0, "volume": 999999}
        assert tracker.is_one_price_limit_up(bar, prev_close=100.0, vol_ma20=None) is True

    def test_data_anomaly_missing_prev_close(self):
        bar = {"high": 110.0, "low": 110.0, "close": 110.0, "volume": 100}
        assert tracker.is_one_price_limit_up(bar, prev_close=None, vol_ma20=10000.0) is False


# ── _check_settlement：defer / hold_pending / exit 三種指令 ──────────

def _active_entry(**overrides) -> dict:
    base = {
        "status": "active",
        "effective_stop_loss": 90.0,
        "target": "150",
        "hold_period": "10",
        "active_days": 2,
        "active_entry_price": 100.0,
        "highest_close_since_active": 100.0,
        "strategy": "動能策略",
    }
    base.update(overrides)
    return base


class TestCheckSettlementLockPaths:
    def test_locked_and_h1_defers(self):
        entry = _active_entry()
        result = tracker._check_settlement(
            entry, price=88.0, today_high=89.0, today_low=88.0,
            open_=89.0, prev_close=98.0, volume=500, vol_ma20=10000.0,
        )
        assert result == ("defer", {"pending_vol_baseline": 10000.0})

    def test_locked_and_not_h1_exits_at_close(self):
        # today_high >= stop_loss（H1 不成立）：鎖死∧¬H1 → 取當日 close 下緣
        entry = _active_entry()
        result = tracker._check_settlement(
            entry, price=88.0, today_high=95.0, today_low=88.0,
            open_=89.0, prev_close=98.0, volume=500, vol_ma20=10000.0,
        )
        assert result == ("exit", tracker.EXIT_LOSS, 88.0, "limit_down_thin_fill")

    def test_not_locked_exits_at_min_stop_open(self):
        # D2 不成立（close != low）→ 未鎖死 → min(stop, open)
        entry = _active_entry()
        result = tracker._check_settlement(
            entry, price=89.0, today_high=95.0, today_low=85.0,
            open_=87.0, prev_close=98.0, volume=500, vol_ma20=10000.0,
        )
        assert result == ("exit", tracker.EXIT_LOSS, 87.0, None)

    def test_not_locked_open_missing_falls_back_to_close(self):
        entry = _active_entry()
        result = tracker._check_settlement(
            entry, price=89.0, today_high=95.0, today_low=85.0,
            open_=None, prev_close=98.0, volume=500, vol_ma20=10000.0,
        )
        # open 缺值 fallback → close(=price=89.0)；min(90, 89.0) = 89.0
        assert result == ("exit", tracker.EXIT_LOSS, 89.0, None)


class TestCheckSettlementPendingPaths:
    def test_pending_still_locked_holds(self):
        entry = _active_entry(pending_exit=True, pending_vol_baseline=8000.0)
        result = tracker._check_settlement(
            entry, price=90.0, today_high=91.0, today_low=90.0,
            open_=91.0, prev_close=100.0, volume=500, vol_ma20=2000.0,
        )
        # 用凍結的 pending_vol_baseline（8000），非本輪即時 vol_ma20（2000）
        assert result == ("hold_pending", None)

    def test_pending_no_longer_locked_exits_at_open(self):
        entry = _active_entry(pending_exit=True, pending_vol_baseline=8000.0)
        result = tracker._check_settlement(
            entry, price=90.0, today_high=91.0, today_low=90.0,
            open_=91.5, prev_close=100.0, volume=5000, vol_ma20=2000.0,
        )
        assert result == ("exit", tracker.EXIT_LOSS, 91.5, "limit_down_deferred")

    def test_pending_ignores_take_profit_and_expiry(self):
        # pending_exit=True 時只看鎖死與否，不看停利/到期（設計 §4.4 優先序 1）
        entry = _active_entry(
            pending_exit=True, pending_vol_baseline=8000.0,
            active_days=999, hold_period="1", target="50",
        )
        result = tracker._check_settlement(
            entry, price=90.0, today_high=91.0, today_low=90.0,
            open_=91.0, prev_close=100.0, volume=500, vol_ma20=2000.0,
        )
        assert result == ("hold_pending", None)


class TestCheckSettlementUnaffectedPaths:
    def test_take_profit_ignores_lock_shape(self):
        # 停利不受漲停鎖死影響（P3）：即使 bar 形狀像一字鎖死，停損未觸發時仍正常停利
        entry = _active_entry()
        result = tracker._check_settlement(
            entry, price=151.0, today_high=155.0, today_low=140.0,
            open_=150.0, prev_close=140.0, volume=100, vol_ma20=10000.0,
        )
        assert result == ("exit", tracker.EXIT_PROFIT, 150.0, None)

    def test_black_swan_prioritizes_stop_loss(self):
        # 同日觸停損又觸停利：保守判停損，先過鎖死檢查（此處未鎖死 → min(stop,open)）
        entry = _active_entry()
        result = tracker._check_settlement(
            entry, price=100.0, today_high=155.0, today_low=85.0,
            open_=95.0, prev_close=140.0, volume=100, vol_ma20=10000.0,
        )
        assert result[0] == "exit" and result[1] == tracker.EXIT_LOSS

    def test_trailing_stop_unaffected(self):
        entry = _active_entry(
            active_entry_price=100.0, highest_close_since_active=115.0,
            effective_stop_loss=50.0,
        )
        result = tracker._check_settlement(
            entry, price=109.0, today_high=110.0, today_low=105.0,
            open_=110.0, prev_close=110.0, volume=100, vol_ma20=10000.0,
        )
        assert result == ("exit", tracker.EXIT_TRAILING, 109.0, None)

    def test_expiry_unaffected(self):
        entry = _active_entry(active_days=10, hold_period="10", effective_stop_loss=50.0, target="200")
        result = tracker._check_settlement(
            entry, price=100.0, today_high=100.0, today_low=95.0,
            open_=100.0, prev_close=100.0, volume=100, vol_ma20=10000.0,
        )
        assert result == ("exit", tracker.EXIT_EXPIRED, 100.0, None)

    def test_non_active_entry_returns_none(self):
        entry = _active_entry(status="watch")
        result = tracker._check_settlement(
            entry, price=88.0, today_high=89.0, today_low=88.0,
            open_=89.0, prev_close=98.0, volume=500, vol_ma20=10000.0,
        )
        assert result is None


# ── run_tracker 主迴圈整合測試：§4.5 新鮮度守衛 / §5.1 gate / §5.2 進場價 ──

@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    """把 tracker 的 data 檔案路徑重導向暫存目錄，避免測試互相汙染或動到真實資料。"""
    monkeypatch.setattr(tracker, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(tracker, "_WATCHLIST_PATH", tmp_path / "watchlist.json")
    monkeypatch.setattr(tracker, "_PERF_PATH", tmp_path / "performance_history.json")
    return tmp_path


def _pending_watchlist_entry(**overrides) -> dict:
    base = {
        "symbol": "1234.TW",
        "name": "測試股",
        "sector": "半導體業",
        "buy_zone": "$95～$100",
        "buy_zone_lower": 95.0,
        "buy_zone_upper": 100.0,
        "target": "150",
        "stop_loss": "90",
        "hold_period": "10",
        "strategy": "動能策略",
        "tracked_dates": ["2026-01-05"],
        "status": "active",
        "invalid_reason": None,
        "slot_blocked_today": False,
        "watch_days": 0,
        "active_days": 5,
        "signal_date_close": 97.0,
        "active_entry_price": 100.0,
        "active_start_date": "2026-01-05",
        "date_added": "2026-01-05",
        "entry_regime": "BULL_TREND",
        "market_breadth_pct": 60.0,
        "vix_value": 18.0,
        "l2_score": 80,
        "ai_confidence": 8,
        "ai_strategy_reason": "",
        "effective_stop_loss": 90.0,
        "planned_stop_loss": 90.0,
        "is_breakeven_locked": False,
        "highest_close_since_active": 100.0,
        "current_price": 88.0,
        "pending_exit": True,
        "pending_vol_baseline": 8000.0,
        "locked_days": 3,
        "pending_stale_runs": 14,
    }
    base.update(overrides)
    return base


class TestRunTrackerFreshnessGuard:
    def test_stale_run_increments_and_force_settles_at_threshold(self, monkeypatch):
        entry = _pending_watchlist_entry(pending_stale_runs=14)
        tracker.save_watchlist([entry])
        monkeypatch.setattr(tracker, "_fetch_latest", lambda symbols: {})  # 無新鮮資料

        watchlist, categories = tracker.run_tracker([], market_date="2026-01-20")

        assert watchlist == []  # 已結算移除
        assert len(categories["settled"]) == 1
        settled = categories["settled"][0]
        assert settled["_exit_reason"] == tracker.EXIT_LOSS
        assert settled["_exit_price"] == 88.0  # entry["current_price"]（上一輪值，未覆寫）

        with open(tracker._PERF_PATH, "r", encoding="utf-8") as f:
            perf = json.load(f)
        record = perf["history_records"][-1]
        assert record["actual_outcome"]["exit_note"] == "force_settled_after_stale_limit"
        assert record["actual_outcome"]["locked_days"] == 3
        assert record["actual_outcome"]["exit_deferred"] is True

    def test_stale_run_below_threshold_holds_without_settling(self, monkeypatch):
        entry = _pending_watchlist_entry(pending_stale_runs=5)
        tracker.save_watchlist([entry])
        monkeypatch.setattr(tracker, "_fetch_latest", lambda symbols: {})

        watchlist, categories = tracker.run_tracker([], market_date="2026-01-20")

        assert len(watchlist) == 1
        assert watchlist[0]["pending_stale_runs"] == 6
        assert watchlist[0]["active_days"] == 6  # 持有計時照常遞增
        assert watchlist[0]["current_price"] == 88.0  # 未覆寫
        assert categories["settled"] == []

    def test_same_day_rerun_does_not_double_count_stale_runs(self, monkeypatch):
        entry = _pending_watchlist_entry(pending_stale_runs=5, tracked_dates=["2026-01-05", "2026-01-20"])
        tracker.save_watchlist([entry])
        monkeypatch.setattr(tracker, "_fetch_latest", lambda symbols: {})

        watchlist, _ = tracker.run_tracker([], market_date="2026-01-20")

        assert watchlist[0]["pending_stale_runs"] == 5  # already_tracked_today → 不遞增
        assert watchlist[0]["active_days"] == 5

    def test_fresh_bar_resolves_pending_and_resets_stale_counter(self, monkeypatch):
        entry = _pending_watchlist_entry(pending_stale_runs=7)
        tracker.save_watchlist([entry])

        def fake_fetch(symbols):
            return {
                "1234.TW": {
                    "price": 91.5, "today_high": 92.0, "today_low": 90.0,
                    "open": 91.5, "prev_close": 100.0, "volume": 6000.0,
                    "vol_ma20": 2000.0,  # 用凍結的 baseline(8000) 判定，非此值
                    "bar_date": "2026-01-20",
                    "ema20": 95.0, "ema50": 90.0,
                    "close_series": None,
                }
            }
        monkeypatch.setattr(tracker, "_fetch_latest", fake_fetch)

        watchlist, categories = tracker.run_tracker([], market_date="2026-01-20")

        assert watchlist == []
        settled = categories["settled"][0]
        assert settled["_exit_reason"] == tracker.EXIT_LOSS
        assert settled["_exit_price"] == 91.5  # 該日 open

        with open(tracker._PERF_PATH, "r", encoding="utf-8") as f:
            perf = json.load(f)
        assert perf["history_records"][-1]["actual_outcome"]["exit_note"] == "limit_down_deferred"

    def test_fresh_bar_still_locked_holds_pending_using_baseline(self, monkeypatch):
        entry = _pending_watchlist_entry(pending_stale_runs=7, locked_days=3)
        tracker.save_watchlist([entry])

        def fake_fetch(symbols):
            return {
                "1234.TW": {
                    "price": 90.0, "today_high": 90.0, "today_low": 90.0,
                    "open": 90.0, "prev_close": 100.0, "volume": 500.0,
                    "vol_ma20": 100000.0,  # 若誤用即時值會判「量能不枯竭」→ 不鎖死；必須用 baseline(8000)
                    "bar_date": "2026-01-20",
                    "ema20": 95.0, "ema50": 90.0,
                    "close_series": None,
                }
            }
        monkeypatch.setattr(tracker, "_fetch_latest", fake_fetch)

        watchlist, categories = tracker.run_tracker([], market_date="2026-01-20")

        assert len(watchlist) == 1
        assert watchlist[0]["pending_exit"] is True
        assert watchlist[0]["locked_days"] == 4
        assert watchlist[0]["pending_stale_runs"] == 0
        assert categories["settled"] == []


class TestGateStatusWatchPrecondition:
    def _watch_entry(self, **overrides):
        base = {
            "symbol": "5678.TW",
            "name": "測試股2",
            "sector": "電子業",
            "buy_zone": "$95～$100",
            "buy_zone_lower": 95.0,
            "buy_zone_upper": 100.0,
            "target": "150",
            "stop_loss": "80",
            "hold_period": "10",
            "strategy": "動能策略",
            "tracked_dates": ["2026-01-05"],
            "status": "watch",
            "invalid_reason": None,
            "slot_blocked_today": False,
            "watch_days": 1,
            "active_days": 0,
            "signal_date_close": 97.0,
            "active_entry_price": None,
            "active_start_date": None,
            "date_added": "2026-01-05",
            "entry_regime": "BULL_TREND",
            "market_breadth_pct": 60.0,
            "vix_value": 18.0,
            "l2_score": 80,
            "ai_confidence": 8,
            "ai_strategy_reason": "",
        }
        base.update(overrides)
        return base

    def _fake_fetch_one_price_limit_up(self, symbols):
        # 一字漲停：high=low=close=110（觸價 today_low<=buy_zone_upper=100？不成立，
        # 但收盤價路徑 price>upper 仍可能判 watch/active，用來驗證 gate 覆寫優先於收盤價狀態機）
        return {
            symbols[0]: {
                "price": 99.0, "today_high": 99.0, "today_low": 99.0,
                "open": 99.0, "prev_close": 90.0, "volume": 500.0,
                "vol_ma20": 10000.0,
                "bar_date": "2026-01-20",
                "ema20": 95.0, "ema50": 90.0,
                "close_series": None,
            }
        }

    def test_watch_entry_blocked_by_one_price_limit_up_gate(self, monkeypatch):
        entry = self._watch_entry()
        tracker.save_watchlist([entry])
        monkeypatch.setattr(
            tracker, "_fetch_latest",
            lambda symbols: self._fake_fetch_one_price_limit_up(symbols),
        )

        watchlist, categories = tracker.run_tracker([], market_date="2026-01-20")

        # today_low(99) <= buy_zone_upper(100) 原本會觸價進場，但一字漲停 gate 覆寫為 watch
        assert watchlist[0]["status"] == "watch"
        assert watchlist[0]["watch_days"] == 2

    def test_active_entry_not_demoted_by_gate(self, monkeypatch):
        # v9 修正的 blocker：gate 只在 status=="watch" 時套用，已持有的 active 部位
        # 即使當日 bar 滿足一字漲停條件，也不能被誤降級為 watch
        entry = self._watch_entry(
            status="active", active_entry_price=95.0, active_start_date="2026-01-05",
            effective_stop_loss=80.0, planned_stop_loss=80.0, is_breakeven_locked=False,
            highest_close_since_active=95.0, active_days=5,
        )
        tracker.save_watchlist([entry])
        monkeypatch.setattr(
            tracker, "_fetch_latest",
            lambda symbols: self._fake_fetch_one_price_limit_up(symbols),
        )

        watchlist, categories = tracker.run_tracker([], market_date="2026-01-20")

        assert watchlist[0]["status"] == "active"
        assert not watchlist[0].get("pending_exit")


class TestEntryFillPriceGapDown:
    def test_entry_fill_price_uses_min_buy_zone_upper_and_open(self, monkeypatch):
        # §5.2：跌停穿越買入區時，成交價 = min(buy_zone_upper, open)，非單純 buy_zone_upper
        entry = {
            "symbol": "9999.TW",
            "name": "測試股3",
            "sector": "電子業",
            "buy_zone": "$95～$100",
            "buy_zone_lower": 95.0,
            "buy_zone_upper": 100.0,
            "target": "150",
            "stop_loss": "80",
            "hold_period": "10",
            "strategy": "動能策略",
            "tracked_dates": ["2026-01-05"],
            "status": "watch",
            "invalid_reason": None,
            "slot_blocked_today": False,
            "watch_days": 1,
            "active_days": 0,
            "signal_date_close": 97.0,
            "active_entry_price": None,
            "active_start_date": None,
            "date_added": "2026-01-05",
            "entry_regime": "BULL_TREND",
            "market_breadth_pct": 60.0,
            "vix_value": 18.0,
            "l2_score": 80,
            "ai_confidence": 8,
            "ai_strategy_reason": "",
        }
        tracker.save_watchlist([entry])

        def fake_fetch(symbols):
            return {
                symbols[0]: {
                    # 跳空開低跌破買入區：today_low(85) <= buy_zone_upper(100) 觸價，
                    # 但 open(88) 遠低於 upper(100)
                    "price": 89.0, "today_high": 91.0, "today_low": 85.0,
                    "open": 88.0, "prev_close": 96.0, "volume": 5000.0,
                    "vol_ma20": 10000.0,
                    "bar_date": "2026-01-20",
                    "ema20": 90.0, "ema50": 88.0,
                    "close_series": None,
                }
            }
        monkeypatch.setattr(tracker, "_fetch_latest", fake_fetch)

        watchlist, categories = tracker.run_tracker([], market_date="2026-01-20")

        assert watchlist[0]["status"] == "active"
        assert watchlist[0]["active_entry_price"] == 88.0  # min(100, 88) = 88


# ── Q1：pending 期間發生真實拆股，pending_vol_baseline 標尺不受 split_factor 影響 ──

class TestPendingSplitFactorScaleImmunity:
    def test_split_factor_does_not_scale_volume_baseline_comparison(self, monkeypatch):
        """
        設計 §9 Q1：驗證即使當輪偵測到拆股（split_factor != 1.0，導致 buy_zone/
        stop_loss/target 等門檻被縮放進 adj 複本），pending 判定使用的
        volume/vol_ma20/pending_vol_baseline 完全不受 split_factor 縮放
        （§4.1 標尺註記：這些欄位一律用原生 volume 值比較）。
        """
        entry = _pending_watchlist_entry(pending_vol_baseline=8000.0, pending_stale_runs=0)
        tracker.save_watchlist([entry])

        # 強制觸發 split_factor 分支：signal_date_close 與 close_series 錨定值差異 >1%
        # （close_series 必須是非 None 值，_calc_split_factor 才會被呼叫到）
        monkeypatch.setattr(tracker, "_calc_split_factor", lambda *a, **kw: 0.5)

        def fake_fetch(symbols):
            return {
                "1234.TW": {
                    # close(90)=low(90)、prev_close(100) 落在 D1/D2 帶內，因此本輪「鎖死與否」
                    # 完全由 D3（量能）決定：volume=6000 相對未縮放 baseline(8000) 判定
                    # 「未鎖死」（6000 > 0.6*8000=4800）；若程式碼誤把 volume 乘上
                    # split_factor(0.5) → 3000 <= 4800 會誤判「鎖死」而錯誤地 hold_pending，
                    # 本測試專挑這個會讓兩種結果分岔的數值窗口來抓這個 bug
                    "price": 90.0, "today_high": 92.0, "today_low": 90.0,
                    "open": 91.0, "prev_close": 100.0, "volume": 6000.0,
                    "vol_ma20": 2000.0,
                    "bar_date": "2026-01-20",
                    "ema20": 95.0, "ema50": 90.0,
                    "close_series": pd.Series([100.0, 99.0], index=pd.to_datetime(["2026-01-19", "2026-01-20"])),
                }
            }
        monkeypatch.setattr(tracker, "_fetch_latest", fake_fetch)

        watchlist, categories = tracker.run_tracker([], market_date="2026-01-20")

        # 用未縮放的 volume(6000) 對比未縮放的 baseline(8000)：6000 > 0.6*8000=4800 → 未鎖死 → 解除結算
        assert watchlist == []
        assert len(categories["settled"]) == 1
        assert categories["settled"][0]["_exit_price"] == 91.0


# ── _fetch_latest（yfinance 批次下載，防禦性欄位缺失處理）────────────

class TestFetchLatestMissingColumns:
    def test_missing_close_column_skips_symbol_not_crashes(self, monkeypatch):
        """yfinance 對個別股票的批次下載可能因暫時性 API 異常回傳不完整欄位
        （例如缺 Close，但 Open/High/Low/Volume 仍在）。_fetch_latest 對
        High/Low/Open/Volume 皆有「欄位不存在→fallback」防禦，但先前 Close
        是直接 df["Close"] 硬讀，缺欄位時整支 KeyError 炸掉 main.py（見
        2026-07-17 實際運行踩雷）。應優雅跳過該股票，其餘股票正常回傳。"""
        idx = pd.date_range("2026-06-01", periods=5, freq="D")
        cols = pd.MultiIndex.from_tuples([
            ("GOOD.TW", "Open"), ("GOOD.TW", "High"), ("GOOD.TW", "Low"),
            ("GOOD.TW", "Close"), ("GOOD.TW", "Volume"),
            ("BAD.TW", "Open"), ("BAD.TW", "High"), ("BAD.TW", "Low"),
            ("BAD.TW", "Volume"),  # 缺 Close
        ])
        data = [[10, 11, 9, 10.5, 1000, 20, 21, 19, 2000]] * 5
        fake_df = pd.DataFrame(data, index=idx, columns=cols)
        monkeypatch.setattr(tracker.yf, "download", lambda **kw: fake_df)

        result = tracker._fetch_latest(["GOOD.TW", "BAD.TW"])

        assert "GOOD.TW" in result
        assert result["GOOD.TW"]["price"] == 10.5
        assert "BAD.TW" not in result


# ── save_watchlist 原子寫入 ───────────────────────────────────────────

def test_save_watchlist_atomic_write_leaves_no_tmp_file(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(tracker, "_WATCHLIST_PATH", tmp_path / "watchlist.json")
    tracker.save_watchlist([{"symbol": "1234.TW"}])
    assert (tmp_path / "watchlist.json").exists()
    assert not (tmp_path / "watchlist.tmp").exists()
    loaded = tracker.load_watchlist()
    assert loaded == [{"symbol": "1234.TW"}]


# ── is_safe_to_run（P6 運行前提）────────────────────────────────────

class TestIsSafeToRun:
    _TZ = __import__("zoneinfo").ZoneInfo("Asia/Taipei")

    def test_weekend_is_not_safe(self):
        import datetime as dt
        saturday = dt.datetime(2026, 1, 17, 14, 0, tzinfo=self._TZ)  # 2026-01-17 是週六
        assert tracker.is_safe_to_run(saturday) is False

    def test_before_market_close_is_not_safe(self):
        import datetime as dt
        weekday_morning = dt.datetime(2026, 1, 20, 10, 0, tzinfo=self._TZ)  # 週二早上
        assert tracker.is_safe_to_run(weekday_morning) is False

    def test_after_market_close_on_weekday_is_safe(self):
        import datetime as dt
        weekday_afternoon = dt.datetime(2026, 1, 20, 14, 0, tzinfo=self._TZ)
        assert tracker.is_safe_to_run(weekday_afternoon) is True

"""publisher.py 單元測試：績效統計、HTML 片段生成（含 Phase 3 pending_exit/exit_note
顯示）、XSS escaping、首頁/報告發布流程、git push 無 remote 時優雅略過。"""

import json
from datetime import datetime

import pandas as pd
import pytest

import publisher


@pytest.fixture(autouse=True)
def _isolate_docs_dir(tmp_path, monkeypatch):
    """把 publisher 的輸出路徑重導向暫存目錄，避免測試污染真實 docs/ 與 data/。"""
    docs = tmp_path / "docs"
    data_dir = tmp_path / "docs" / "data"
    monkeypatch.setattr(publisher, "_ROOT", tmp_path)
    monkeypatch.setattr(publisher, "_DOCS", docs)
    monkeypatch.setattr(publisher, "_REPORTS_DIR", docs / "reports")
    monkeypatch.setattr(publisher, "_DATA_DIR", data_dir)
    monkeypatch.setattr(publisher, "_INDEX_JSON", data_dir / "reports-index.json")
    monkeypatch.setattr(publisher, "_LAST_RUN_JSON", data_dir / "last_run.json")
    monkeypatch.setattr(publisher, "_INDEX_HTML", docs / "index.html")
    monkeypatch.setattr(publisher, "_PERF_PATH", tmp_path / "data" / "performance_history.json")
    return tmp_path


class TestEsc:
    def test_escapes_html_special_chars(self):
        assert publisher._esc('<script>alert("x")&y</script>') == \
            '&lt;script&gt;alert("x")&amp;y&lt;/script&gt;'

    def test_non_string_input_coerced(self):
        assert publisher._esc(123) == "123"


class TestPerformanceStats:
    def test_no_file_returns_default_zero(self):
        stats = publisher._load_performance_stats()
        assert stats == {"total": 0, "win_rate": 0.0, "avg_return": 0.0, "by_strategy": {}}

    def test_corrupt_file_returns_default(self, tmp_path):
        publisher._PERF_PATH.parent.mkdir(parents=True, exist_ok=True)
        publisher._PERF_PATH.write_text("not json", encoding="utf-8")
        assert publisher._load_performance_stats()["total"] == 0

    def test_computes_win_rate_and_avg_return(self):
        records = [
            {
                "signal_details": {"assigned_strategy": "動能策略"},
                "actual_outcome": {"exit_reason": "CLOSED_PROFIT"},
                "performance_metrics": {"is_win": True, "return_pct": 10.0},
            },
            {
                "signal_details": {"assigned_strategy": "動能策略"},
                "actual_outcome": {"exit_reason": "CLOSED_LOSS"},
                "performance_metrics": {"is_win": False, "return_pct": -5.0},
            },
        ]
        publisher._PERF_PATH.parent.mkdir(parents=True, exist_ok=True)
        publisher._PERF_PATH.write_text(
            json.dumps({"history_records": records}, ensure_ascii=False), encoding="utf-8"
        )
        stats = publisher._load_performance_stats()
        assert stats["total"] == 2
        assert stats["win_rate"] == 50.0
        assert stats["avg_return"] == 2.5
        assert stats["by_strategy"]["動能策略"] == 50.0

    def test_ignores_non_terminal_records(self):
        records = [{"actual_outcome": {"exit_reason": "SOMETHING_ELSE"}}]
        publisher._PERF_PATH.parent.mkdir(parents=True, exist_ok=True)
        publisher._PERF_PATH.write_text(
            json.dumps({"history_records": records}, ensure_ascii=False), encoding="utf-8"
        )
        assert publisher._load_performance_stats()["total"] == 0


class TestBuildPerformanceSection:
    def test_empty_when_zero_total(self):
        assert publisher._build_performance_section({"total": 0}) == ""

    def test_renders_stats_when_present(self):
        html = publisher._build_performance_section({
            "total": 10, "win_rate": 60.0, "avg_return": 3.5, "by_strategy": {"動能策略": 70.0},
        })
        assert "60.0%" in html
        assert "+3.50%" in html
        assert "動能策略勝率" in html


class TestGetDailyChange:
    def test_missing_price_data_returns_flat(self):
        assert publisher._get_daily_change({}) == (0.0, "▬", "flat")

    def test_up_day(self):
        df = pd.DataFrame({"Close": [100.0, 105.0]})
        pct, sign, cls = publisher._get_daily_change({"_price_data": df})
        assert cls == "up" and sign == "▲"
        assert pct == pytest.approx(5.0)

    def test_down_day(self):
        df = pd.DataFrame({"Close": [100.0, 95.0]})
        pct, sign, cls = publisher._get_daily_change({"_price_data": df})
        assert cls == "down" and sign == "▼"


class TestTrackingRowPendingExit:
    def _active_entry(self, **overrides):
        base = {
            "symbol": "1234.TW", "name": "測試股", "strategy": "動能策略",
            "tracked_dates": ["2026-01-01", "2026-01-02"],
            "current_price": 95.0, "active_days": 3, "hold_period": "10",
            "active_entry_price": 100.0, "target": "NT$120", "stop_loss": "NT$90",
            "effective_stop_loss": 90.0, "is_breakeven_locked": False,
        }
        base.update(overrides)
        return base

    def test_pending_exit_shows_tag(self):
        entry = self._active_entry(pending_exit=True, locked_days=2)
        html = publisher._tracking_row(entry, "active")
        assert "跌停鎖死排隊中" in html
        assert "第 2 天" in html

    def test_non_pending_active_no_tag(self):
        entry = self._active_entry()
        html = publisher._tracking_row(entry, "active")
        assert "跌停鎖死排隊中" not in html

    def test_breakeven_lock_tag_shown(self):
        entry = self._active_entry(is_breakeven_locked=True)
        html = publisher._tracking_row(entry, "active")
        assert "🔒保本" in html

    def test_watch_status_slot_blocked(self):
        entry = self._active_entry(slot_blocked_today=True, watch_days=1, buy_zone="NT$95～NT$100")
        html = publisher._tracking_row(entry, "watch")
        assert "今日觸價但未在掛單名單" in html


class TestSettledRowExitNote:
    def _settled_entry(self, exit_reason, exit_note=None, **overrides):
        base = {
            "symbol": "1234.TW", "name": "測試股", "strategy": "動能策略",
            "_exit_reason": exit_reason, "_exit_note": exit_note,
            "_exit_price": 88.0, "active_entry_price": 100.0, "active_days": 5,
        }
        base.update(overrides)
        return base

    def test_limit_down_thin_fill_overrides_generic_loss_label(self):
        entry = self._settled_entry("CLOSED_LOSS", exit_note="limit_down_thin_fill")
        html = publisher._settled_row(entry)
        assert "跌停鎖死無量陰跌" in html
        assert "觸發止損，停損出場" not in html

    def test_limit_down_deferred_label(self):
        entry = self._settled_entry("CLOSED_LOSS", exit_note="limit_down_deferred")
        html = publisher._settled_row(entry)
        assert "跌停鎖死解除" in html

    def test_force_settled_after_stale_limit_label(self):
        entry = self._settled_entry("CLOSED_LOSS", exit_note="force_settled_after_stale_limit")
        html = publisher._settled_row(entry)
        assert "強制以最後已知價結算" in html

    def test_generic_closed_loss_without_exit_note(self):
        entry = self._settled_entry("CLOSED_LOSS")
        html = publisher._settled_row(entry)
        assert "觸發止損，停損出場" in html

    def test_closed_profit_label(self):
        entry = self._settled_entry("CLOSED_PROFIT")
        html = publisher._settled_row(entry)
        assert "達到目標價，停利出場" in html

    def test_pnl_computed_correctly(self):
        entry = self._settled_entry("CLOSED_LOSS", active_entry_price=100.0)
        entry["_exit_price"] = 88.0
        html = publisher._settled_row(entry)
        assert "-12.00%" in html


class TestMarketDashboard:
    def test_empty_context_returns_empty_string(self):
        assert publisher._build_market_dashboard({}) == ""

    def test_renders_regime_name_and_vix_source_label(self):
        html = publisher._build_market_dashboard({
            "regime": "BULL_TREND",
            "market_breadth_pct": 65.0,
            "primary_strategy": "動能策略",
            "vix": {"value": 15.0, "label": "正常", "source": "taifex"},
            "index": {"above_ema20": True},
        })
        assert "強勢牛市" in html
        assert "真VIX" in html
        assert "65.0%" in html

    def test_hv20_fallback_source_label(self):
        html = publisher._build_market_dashboard({
            "regime": "BEAR_DISTRIBUTION",
            "market_breadth_pct": 20.0,
            "primary_strategy": "",
            "vix": {"value": 30.0, "label": "高度恐慌", "source": "hv20_fallback"},
        })
        assert "HV20替代" in html
        assert "全面防禦" in html


class TestBuildDailyReportSmoke:
    def test_produces_valid_html_with_sections(self):
        categories = {
            "active": [{
                "symbol": "1234.TW", "name": "測試股", "strategy": "動能策略",
                "tracked_dates": ["2026-01-01"], "current_price": 105.0,
                "active_days": 2, "hold_period": "10", "active_entry_price": 100.0,
                "target": "NT$120", "stop_loss": "NT$90", "effective_stop_loss": 90.0,
            }],
            "watch": [], "invalid": [], "expired": [], "settled": [], "new": [], "reset": [],
            "order_plan": {"free_slots": 2, "roster": []},
        }
        stats = {"total": 150, "l1_count": 100, "l2_count": 50, "ai_count": 3}
        html = publisher._build_daily_report(categories, stats, "2026-01-20", "二", market_context={})
        assert "<!DOCTYPE html>" in html
        assert "1234.TW" in html
        assert "台股 AI 選股報告" in html

    def test_bear_distribution_defense_banner(self):
        categories = {
            "active": [], "watch": [], "invalid": [], "expired": [], "settled": [],
            "new": [], "reset": [], "order_plan": {},
        }
        stats = {"total": 150, "l1_count": 100, "l2_count": 0, "ai_count": 0}
        html = publisher._build_daily_report(
            categories, stats, "2026-01-20", "二",
            market_context={"regime": "BEAR_DISTRIBUTION", "market_breadth_pct": 15.0},
        )
        assert "系統啟動全面防禦" in html


class TestSyncIndex:
    def test_writes_index_html(self):
        publisher._DOCS.mkdir(parents=True, exist_ok=True)
        publisher.sync_index()
        assert publisher._INDEX_HTML.exists()
        content = publisher._INDEX_HTML.read_text(encoding="utf-8")
        assert "台股 AI 選股系統" in content


class TestGitRemoteAndPush:
    def test_check_git_remote_false_outside_repo(self, tmp_path, monkeypatch):
        # tmp_path 不是 git repo，git remote 應回傳空字串 → False
        monkeypatch.setattr(publisher, "_ROOT", tmp_path)
        assert publisher._check_git_remote() is False


class TestPublishDryRun:
    def test_dry_run_skips_git_push_and_writes_artifacts(self, monkeypatch):
        push_called = {"n": 0}
        monkeypatch.setattr(publisher, "_git_push", lambda *a, **kw: push_called.__setitem__("n", push_called["n"] + 1))
        categories = {
            "active": [], "watch": [], "invalid": [], "expired": [], "settled": [],
            "new": [], "reset": [], "order_plan": {},
        }
        stats = {
            "total": 150, "l1_count": 100, "l2_count": 50, "ai_count": 0,
            "date": datetime(2026, 1, 20),
        }
        publisher.publish(categories, stats, dry_run=True, market_context={})

        assert push_called["n"] == 0
        assert (publisher._REPORTS_DIR / "2026-01-20.html").exists()
        assert publisher._LAST_RUN_JSON.exists()
        assert publisher._INDEX_JSON.exists()

    def test_non_dry_run_skips_push_without_remote(self, monkeypatch):
        push_called = {"n": 0}
        monkeypatch.setattr(publisher, "_git_push", lambda *a, **kw: push_called.__setitem__("n", push_called["n"] + 1))
        monkeypatch.setattr(publisher, "_check_git_remote", lambda: False)
        categories = {
            "active": [], "watch": [], "invalid": [], "expired": [], "settled": [],
            "new": [], "reset": [], "order_plan": {},
        }
        stats = {"total": 1, "l1_count": 1, "l2_count": 1, "ai_count": 0, "date": datetime(2026, 1, 20)}
        publisher.publish(categories, stats, dry_run=False, market_context={})
        assert push_called["n"] == 0

    def test_index_entry_updated_not_duplicated_on_rerun(self):
        categories = {
            "active": [{"symbol": "1234.TW"}], "watch": [], "invalid": [], "expired": [],
            "settled": [], "new": [], "reset": [], "order_plan": {},
        }
        stats = {"total": 1, "l1_count": 1, "l2_count": 1, "ai_count": 0, "date": datetime(2026, 1, 20)}
        publisher.publish(categories, stats, dry_run=True, market_context={})
        publisher.publish(categories, stats, dry_run=True, market_context={})

        with open(publisher._INDEX_JSON, encoding="utf-8") as f:
            index = json.load(f)
        assert len(index) == 1
        assert index[0]["active"] == 1

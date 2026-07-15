"""disposition.py 單元測試：ROC 日期區間解析、處置股/分盤集合競價現況排除、
網路失敗優雅降級。"""

from datetime import date

import pytest

import disposition


class TestParseRocPeriod:
    def test_typical_range(self):
        result = disposition._parse_roc_period("115/07/03～115/07/16")
        assert result == (date(2026, 7, 3), date(2026, 7, 16))

    def test_single_digit_month_day(self):
        result = disposition._parse_roc_period("115/7/3～115/7/16")
        assert result == (date(2026, 7, 3), date(2026, 7, 16))

    def test_malformed_string_returns_none(self):
        assert disposition._parse_roc_period("garbage") is None
        assert disposition._parse_roc_period("") is None
        assert disposition._parse_roc_period(None) is None

    def test_invalid_calendar_date_returns_none(self):
        # 115/13/40 不是合法日期
        assert disposition._parse_roc_period("115/13/40～115/07/16") is None

    def test_different_separator_still_parses(self):
        # 泛用 \D+ 匹配任何非數字分隔符，不假設固定符號
        result = disposition._parse_roc_period("115/07/03~115/07/16")
        assert result == (date(2026, 7, 3), date(2026, 7, 16))


class TestFetchActiveDispositionSymbols(object):
    def test_active_period_included(self, monkeypatch):
        rows = [
            {"Code": "2303", "Name": "聯電", "DispositionPeriod": "115/07/02～115/07/15",
             "DispositionMeasures": "第一次處置"},
        ]
        monkeypatch.setattr(disposition, "requests", _FakeRequests(rows))
        excluded = disposition.fetch_active_disposition_symbols(today=date(2026, 7, 10))
        assert "2303.TW" in excluded
        assert "處置股" in excluded["2303.TW"]

    def test_period_not_yet_started_excluded_from_result(self, monkeypatch):
        rows = [
            {"Code": "2303", "Name": "聯電", "DispositionPeriod": "115/07/20～115/08/02",
             "DispositionMeasures": "第一次處置"},
        ]
        monkeypatch.setattr(disposition, "requests", _FakeRequests(rows))
        excluded = disposition.fetch_active_disposition_symbols(today=date(2026, 7, 10))
        assert excluded == {}

    def test_period_already_ended_excluded_from_result(self, monkeypatch):
        rows = [
            {"Code": "2303", "Name": "聯電", "DispositionPeriod": "115/06/01～115/06/14",
             "DispositionMeasures": "第一次處置"},
        ]
        monkeypatch.setattr(disposition, "requests", _FakeRequests(rows))
        excluded = disposition.fetch_active_disposition_symbols(today=date(2026, 7, 10))
        assert excluded == {}

    def test_boundary_dates_inclusive(self, monkeypatch):
        rows = [
            {"Code": "2303", "Name": "聯電", "DispositionPeriod": "115/07/02～115/07/15",
             "DispositionMeasures": "第一次處置"},
        ]
        monkeypatch.setattr(disposition, "requests", _FakeRequests(rows))
        # 起始日與結束日皆應視為仍在處置期間內
        assert "2303.TW" in disposition.fetch_active_disposition_symbols(today=date(2026, 7, 2))
        assert "2303.TW" in disposition.fetch_active_disposition_symbols(today=date(2026, 7, 15))
        assert "2303.TW" not in disposition.fetch_active_disposition_symbols(today=date(2026, 7, 16))

    def test_malformed_period_does_not_exclude(self, monkeypatch):
        rows = [
            {"Code": "2303", "Name": "聯電", "DispositionPeriod": "garbage",
             "DispositionMeasures": "第一次處置"},
        ]
        monkeypatch.setattr(disposition, "requests", _FakeRequests(rows))
        assert disposition.fetch_active_disposition_symbols(today=date(2026, 7, 10)) == {}

    def test_network_failure_returns_empty_dict(self, monkeypatch):
        monkeypatch.setattr(disposition, "requests", _FailingRequests())
        assert disposition.fetch_active_disposition_symbols(today=date(2026, 7, 10)) == {}

    def test_covered_warrant_code_not_ordinary_stock_still_returned_raw(self, monkeypatch):
        # disposition.py 本身不過濾非普通股代碼（如權證的6碼代號），
        # 交由呼叫端（filter.py 的 price_data 只含普通股）自然忽略不相關的 key
        rows = [
            {"Code": "052974", "Name": "今國光統一5C購02", "DispositionPeriod": "115/07/15～115/07/28",
             "DispositionMeasures": "第一次處置"},
        ]
        monkeypatch.setattr(disposition, "requests", _FakeRequests(rows))
        excluded = disposition.fetch_active_disposition_symbols(today=date(2026, 7, 20))
        assert "052974.TW" in excluded


class TestFetchBatchAuctionSymbols:
    def test_non_blank_marker_excluded(self, monkeypatch):
        rows = [
            {"Code": "2314", "Name": "台揚", "PeriodicCallAuctionTrading": "**"},
            {"Code": "1213", "Name": "大飲", "PeriodicCallAuctionTrading": "  "},
        ]
        monkeypatch.setattr(disposition, "requests", _FakeRequests(rows))
        excluded = disposition.fetch_batch_auction_symbols()
        assert "2314.TW" in excluded
        assert "1213.TW" not in excluded

    def test_network_failure_returns_empty_dict(self, monkeypatch):
        monkeypatch.setattr(disposition, "requests", _FailingRequests())
        assert disposition.fetch_batch_auction_symbols() == {}


class TestFetchExcludedSymbolsUnion:
    def test_union_of_both_sources(self, monkeypatch):
        punish_rows = [
            {"Code": "2303", "Name": "聯電", "DispositionPeriod": "115/07/02～115/07/15",
             "DispositionMeasures": "第一次處置"},
        ]
        auction_rows = [
            {"Code": "2314", "Name": "台揚", "PeriodicCallAuctionTrading": "**"},
        ]

        call_count = {"n": 0}

        class _Router:
            def get(self, url, timeout=30):
                call_count["n"] += 1
                if "punish" in url:
                    return _FakeResponse(punish_rows)
                return _FakeResponse(auction_rows)

        monkeypatch.setattr(disposition, "requests", _Router())
        excluded = disposition.fetch_excluded_symbols(today=date(2026, 7, 10))
        assert set(excluded.keys()) == {"2303.TW", "2314.TW"}

    def test_disposition_reason_wins_on_overlap(self, monkeypatch):
        # 同一 symbol 兩份清單皆命中時，保留處置股（先寫入）的原因字串
        punish_rows = [
            {"Code": "2303", "Name": "聯電", "DispositionPeriod": "115/07/02～115/07/15",
             "DispositionMeasures": "第一次處置"},
        ]
        auction_rows = [
            {"Code": "2303", "Name": "聯電", "PeriodicCallAuctionTrading": "**"},
        ]

        class _Router:
            def get(self, url, timeout=30):
                if "punish" in url:
                    return _FakeResponse(punish_rows)
                return _FakeResponse(auction_rows)

        monkeypatch.setattr(disposition, "requests", _Router())
        excluded = disposition.fetch_excluded_symbols(today=date(2026, 7, 10))
        assert "處置股" in excluded["2303.TW"]


# ── 測試替身 ──────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeRequests:
    def __init__(self, payload):
        self._payload = payload

    def get(self, url, timeout=30):
        return _FakeResponse(self._payload)


class _FailingRequests:
    def get(self, url, timeout=30):
        raise ConnectionError("network unreachable")

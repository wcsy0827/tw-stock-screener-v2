"""驗證 universe.py 的 TWSE API 請求在遇到暫時性網路錯誤時會重試，而非直接讓
main.py 整支崩潰——這是 2026-08-07 GitHub Actions 排程首次執行實際觸發的
ConnectionError（同一台 runner、同一個 host，前一個請求成功、後一個請求
Connection refused，判定為暫時性網路問題而非 TWSE 端封鎖雲端 IP）。"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest
import requests

import universe


def _make_response(payload):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


class TestGetWithRetry:
    def test_succeeds_first_try_without_retry(self):
        ok_response = _make_response([])
        with patch("universe.requests.get", return_value=ok_response) as mock_get, \
             patch("universe.time.sleep") as mock_sleep:
            resp = universe._get_with_retry("https://example.test")
        assert resp is ok_response
        assert mock_get.call_count == 1
        mock_sleep.assert_not_called()

    def test_retries_after_transient_connection_error_then_succeeds(self):
        ok_response = _make_response([])
        with patch(
            "universe.requests.get",
            side_effect=[requests.exceptions.ConnectionError("Connection refused"), ok_response],
        ) as mock_get, patch("universe.time.sleep") as mock_sleep:
            resp = universe._get_with_retry("https://example.test")
        assert resp is ok_response
        assert mock_get.call_count == 2
        mock_sleep.assert_called_once()

    def test_raises_after_exhausting_all_retries(self):
        with patch(
            "universe.requests.get",
            side_effect=requests.exceptions.ConnectionError("Connection refused"),
        ) as mock_get, patch("universe.time.sleep"):
            with pytest.raises(requests.exceptions.ConnectionError):
                universe._get_with_retry("https://example.test")
        assert mock_get.call_count == universe.MAX_RETRIES


class TestFetchDailyTradeValueRetries:
    def test_fetch_daily_trade_value_survives_one_transient_failure(self):
        ok_response = _make_response(
            [{"Code": "2330", "TradeValue": "1000000000"}]
        )
        with patch(
            "universe.requests.get",
            side_effect=[requests.exceptions.ConnectionError("Connection refused"), ok_response],
        ), patch("universe.time.sleep"):
            result = universe.fetch_daily_trade_value()
        assert result == {"2330": 1000000000.0}


class TestFetchCompanyDirectoryRetries:
    def test_fetch_company_directory_survives_one_transient_failure(self):
        company_response = _make_response(
            [{"公司代號": "2330", "公司簡稱": "台積電", "產業別": "24"}]
        )
        with patch(
            "universe.requests.get",
            side_effect=[requests.exceptions.ConnectionError("Connection refused"), company_response],
        ) as mock_get, patch("universe.time.sleep"), patch(
            "universe.fetch_industry_names", return_value={}
        ):
            result = universe.fetch_company_directory()
        assert result["2330"]["name"] == "台積電"
        assert mock_get.call_count == 2

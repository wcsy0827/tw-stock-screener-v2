"""filter.py 單元測試，聚焦新增的 excluded_symbols（處置股/分盤集合競價排除）
參數，並涵蓋既有 L1 篩選條件的基本回歸（此 repo 先前沒有 test_filter.py）。"""

import numpy as np
import pandas as pd
import pytest

import filter as filter_


def _passing_df(n: int = 40, close: float = 100.0, volume: float = 20_000_000) -> pd.DataFrame:
    """建構一份預設會通過所有 L1 條件的 OHLCV DataFrame（低波動、高量、高價）。"""
    idx = pd.date_range("2026-05-01", periods=n, freq="B")
    closes = np.full(n, close)
    return pd.DataFrame({
        "Close":  closes,
        "High":   closes * 1.001,
        "Low":    closes * 0.999,
        "Volume": np.full(n, volume),
    }, index=idx)


def _passing_info(market_cap: float = 5e10) -> dict:
    return {"market_cap": market_cap}


class TestExcludedSymbols:
    def test_excluded_symbol_is_filtered_out(self):
        price_data = {"2303.TW": _passing_df()}
        info_data = {"2303.TW": _passing_info()}
        result = filter_.apply_filters(price_data, info_data, excluded_symbols={"2303.TW": "處置股"})
        assert result == []

    def test_non_excluded_symbol_unaffected(self):
        price_data = {"2303.TW": _passing_df(), "2330.TW": _passing_df()}
        info_data = {"2303.TW": _passing_info(), "2330.TW": _passing_info()}
        result = filter_.apply_filters(price_data, info_data, excluded_symbols={"2303.TW": "處置股"})
        assert result == ["2330.TW"]

    def test_none_excluded_symbols_backward_compatible(self):
        # 舊呼叫端不傳 excluded_symbols 仍應正常運作（預設 None → 視為空字典）
        price_data = {"2330.TW": _passing_df()}
        info_data = {"2330.TW": _passing_info()}
        result = filter_.apply_filters(price_data, info_data)
        assert result == ["2330.TW"]

    def test_empty_dict_excluded_symbols_is_noop(self):
        price_data = {"2330.TW": _passing_df()}
        info_data = {"2330.TW": _passing_info()}
        result = filter_.apply_filters(price_data, info_data, excluded_symbols={})
        assert result == ["2330.TW"]


class TestExistingL1Conditions:
    def test_low_price_excluded(self):
        price_data = {"1234.TW": _passing_df(close=5.0)}
        info_data = {"1234.TW": _passing_info()}
        assert filter_.apply_filters(price_data, info_data) == []

    def test_low_trade_value_excluded(self):
        price_data = {"1234.TW": _passing_df(volume=1000)}
        info_data = {"1234.TW": _passing_info()}
        assert filter_.apply_filters(price_data, info_data) == []

    def test_missing_market_cap_excluded(self):
        price_data = {"1234.TW": _passing_df()}
        info_data = {"1234.TW": {"market_cap": None}}
        assert filter_.apply_filters(price_data, info_data) == []

    def test_small_market_cap_excluded(self):
        price_data = {"1234.TW": _passing_df()}
        info_data = {"1234.TW": _passing_info(market_cap=1e9)}
        assert filter_.apply_filters(price_data, info_data) == []

    def test_insufficient_trading_days_excluded(self):
        price_data = {"1234.TW": _passing_df(n=3)}
        info_data = {"1234.TW": _passing_info()}
        assert filter_.apply_filters(price_data, info_data) == []

    def test_healthy_stock_passes(self):
        price_data = {"1234.TW": _passing_df()}
        info_data = {"1234.TW": _passing_info()}
        assert filter_.apply_filters(price_data, info_data) == ["1234.TW"]

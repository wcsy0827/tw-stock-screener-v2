"""ranker.py 單元測試：指標計算、策略標籤、RS_vs_Sector（複用 scorer.py 產業籃子）、
產業多樣性保護、fallback 路徑、DeepSeek 呼叫（mocked）。"""

import json

import numpy as np
import pandas as pd
import pytest

import ranker


def _make_df(n=80, start=100.0, trend=0.3, seed=1, high_low_pad=1.0, volume=1_000_000.0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-01", periods=n, freq="B")
    closes = start + np.cumsum(rng.normal(trend, 1.0, n))
    closes = np.maximum(closes, 1.0)
    highs = closes + high_low_pad
    lows = closes - high_low_pad
    volumes = np.full(n, volume)
    return pd.DataFrame({"Close": closes, "High": highs, "Low": lows, "Volume": volumes}, index=idx)


class TestComputeIndicators:
    def test_basic_fields_present(self):
        df = _make_df(n=80, trend=0.5)
        indic = ranker.compute_indicators("1234.TW", df)
        assert indic["symbol"] == "1234.TW"
        assert isinstance(indic["price"], float)
        assert indic["ema5"] is not None
        assert indic["ema50"] is not None
        assert indic["atr14"] is not None and indic["atr14"] > 0

    def test_beta_none_without_twii_series(self):
        df = _make_df(n=80)
        indic = ranker.compute_indicators("1234.TW", df, r_twii_global=None)
        assert indic["beta_60d"] is None

    def test_beta_computed_with_twii_series(self):
        df = _make_df(n=80, seed=2)
        twii_close = _make_df(n=80, seed=3)["Close"]
        r_twii = twii_close.pct_change().dropna()
        indic = ranker.compute_indicators("1234.TW", df, r_twii_global=r_twii)
        assert indic["beta_60d"] is not None

    def test_short_history_degrades_gracefully(self):
        df = _make_df(n=10)
        indic = ranker.compute_indicators("1234.TW", df)
        assert indic["ema50"] is None
        assert indic["atr14"] is None
        assert indic["momentum_atr"] is None


class TestTagFunctions:
    def test_ma_trend_bull_1(self):
        assert ranker._ma_trend_tag(110, 105, 100, 95) == "BULL_1"

    def test_ma_trend_bull_2(self):
        assert ranker._ma_trend_tag(102, 98, 100, 95) == "BULL_2"

    def test_ma_trend_bear(self):
        assert ranker._ma_trend_tag(90, 92, 95, 100) == "BEAR"

    def test_ma_trend_na_on_missing_values(self):
        assert ranker._ma_trend_tag(None, 98, 100, 95) == "N/A"
        assert ranker._ma_trend_tag(float("nan"), 98, 100, 95) == "N/A"

    def test_macd_hist_tag_short_series_na(self):
        close = pd.Series(np.arange(10, dtype=float) + 100)
        assert ranker._macd_hist_tag(close) == "N/A"

    def test_macd_hist_tag_pos_inc(self):
        close = pd.Series(100 + np.cumsum(np.full(50, 1.0)))
        assert ranker._macd_hist_tag(close) in ("POS_INC", "POS_DEC")

    def test_strategy_tag_reversal(self):
        indic = {"rsi": 40.0, "volume_ratio": 1.0, "stoch_k": 20.0, "dist_from_20d_high_pct": -10.0, "rsi_5d_ago": 30.0}
        assert ranker._strategy_tag(indic) == "REVERSAL"

    def test_strategy_tag_breakout(self):
        indic = {"rsi": 60.0, "volume_ratio": 2.5, "stoch_k": 70.0, "dist_from_20d_high_pct": 0.5, "rsi_5d_ago": 55.0}
        assert ranker._strategy_tag(indic) == "BREAKOUT"

    def test_strategy_tag_momentum(self):
        indic = {
            "rsi": 60.0, "volume_ratio": 2.0, "stoch_k": 70.0, "dist_from_20d_high_pct": -10.0,
            "rsi_5d_ago": 55.0, "ema5": 110, "ema20": 105, "ema50": 100,
        }
        assert ranker._strategy_tag(indic) == "MOMENTUM"

    def test_strategy_tag_neutral_default(self):
        indic = {"rsi": 50.0, "volume_ratio": 0.5, "stoch_k": 50.0, "dist_from_20d_high_pct": -20.0}
        assert ranker._strategy_tag(indic) == "NEUTRAL"


class TestCalcRsVsSector:
    def test_uses_industry_basket_when_available(self):
        sym = "1234.TW"
        df = _make_df(seed=10, trend=1.0)
        peer1 = _make_df(seed=11, trend=0.2)
        peer2 = _make_df(seed=12, trend=0.2)
        peer3 = _make_df(seed=13, trend=0.2)
        price_data = {sym: df, "1111.TW": peer1, "2222.TW": peer2, "3333.TW": peer3}
        sector_map = {sym: "半導體業", "1111.TW": "半導體業", "2222.TW": "半導體業", "3333.TW": "半導體業"}
        rs = ranker._calc_rs_vs_sector(sym, df, "半導體業", price_data, sector_map)
        assert rs is not None

    def test_falls_back_to_twii_when_basket_insufficient(self):
        sym = "1234.TW"
        df = _make_df(seed=20)
        twii_df = _make_df(seed=21)
        price_data = {sym: df, "^TWII": twii_df}
        sector_map = {sym: "半導體業"}  # 無同產業同儕，樣本不足
        rs = ranker._calc_rs_vs_sector(sym, df, "半導體業", price_data, sector_map)
        assert rs is not None

    def test_none_when_no_benchmark_available_at_all(self):
        sym = "1234.TW"
        df = _make_df(seed=30)
        price_data = {sym: df}  # 無同儕也無 ^TWII
        sector_map = {sym: "半導體業"}
        rs = ranker._calc_rs_vs_sector(sym, df, "半導體業", price_data, sector_map)
        assert rs is None

    def test_none_when_stock_history_too_short(self):
        sym = "1234.TW"
        df = _make_df(n=3)
        price_data = {sym: df}
        assert ranker._calc_rs_vs_sector(sym, df, "半導體業", price_data, {}) is None


class TestDiversifyCandidates:
    def _candidates(self, n_per_sector=10):
        result = []
        for i in range(n_per_sector):
            result.append({"symbol": f"A{i}.TW", "sector": "半導體業", "total_score": 90 - i})
        for i in range(n_per_sector):
            result.append({"symbol": f"B{i}.TW", "sector": "電子業", "total_score": 80 - i})
        return result

    def test_caps_per_sector(self):
        candidates = self._candidates()
        result = ranker._diversify_candidates(candidates, regime="BULL_TREND", max_per_sector=3)
        semi = [c for c in result if c["sector"] == "半導體業"]
        elec = [c for c in result if c["sector"] == "電子業"]
        assert len(semi) == 3
        assert len(elec) == 3

    def test_panic_reversal_force_pass_bypasses_cap(self):
        candidates = self._candidates()
        # 加入低分強制放行的反轉股（total_score < 40）
        candidates.append({"symbol": "FORCE.TW", "sector": "半導體業", "total_score": 20.0})
        result = ranker._diversify_candidates(candidates, regime="PANIC_REVERSAL", max_per_sector=3)
        symbols = {c["symbol"] for c in result}
        assert "FORCE.TW" in symbols
        semi = [c for c in result if c["sector"] == "半導體業"]
        # 強制放行不佔用配額，正常配額仍是 3 支 + 1 支強制放行 = 4
        assert len(semi) == 4


class TestEnrichFallback:
    def test_populates_expected_fields(self):
        candidates = [{"symbol": "1234.TW", "sector": "半導體業", "total_score": 75.0, "price": 100.0}]
        info_data = {"1234.TW": {"name": "測試股", "sector": "半導體業"}}
        price_data = {"1234.TW": _make_df(n=5)}
        result = ranker._enrich_fallback(candidates, info_data, price_data)
        assert len(result) == 1
        r = result[0]
        assert r["is_fallback"] is True
        assert r["rank"] == 1
        assert r["name"] == "測試股"
        assert r["confidence"] == 5
        assert r["_price_data"] is not None


class TestRankCandidatesGuards:
    def test_no_candidates_returns_empty(self, monkeypatch):
        monkeypatch.setattr(ranker, "DEEPSEEK_API_KEY", "fake-key")
        assert ranker.rank_candidates([], {}, {}) == []

    def test_no_api_key_uses_fallback(self, monkeypatch):
        monkeypatch.setattr(ranker, "DEEPSEEK_API_KEY", "")
        candidates = [{"symbol": "1234.TW", "sector": "半導體業", "total_score": 80.0, "price": 100.0}]
        info_data = {"1234.TW": {"name": "測試股"}}
        price_data = {"1234.TW": _make_df(n=5)}
        result = ranker.rank_candidates(candidates, price_data, info_data, top_n=3)
        assert len(result) == 1
        assert result[0]["is_fallback"] is True

    def test_bear_distribution_returns_empty_without_calling_api(self, monkeypatch):
        monkeypatch.setattr(ranker, "DEEPSEEK_API_KEY", "fake-key")
        called = {"n": 0}
        monkeypatch.setattr(ranker, "_call_deepseek", lambda *a, **kw: called.__setitem__("n", called["n"] + 1) or [])
        candidates = [{"symbol": "1234.TW", "sector": "半導體業", "total_score": 80.0, "price": 100.0}]
        result = ranker.rank_candidates(
            candidates, {"1234.TW": _make_df(n=5)}, {},
            market_context={"regime": "BEAR_DISTRIBUTION"},
        )
        assert result == []
        assert called["n"] == 0

    def test_ai_failure_falls_back(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ranker, "DEEPSEEK_API_KEY", "fake-key")
        monkeypatch.setattr(ranker, "_CACHE_DIR", tmp_path)
        monkeypatch.setattr(ranker, "_call_deepseek", lambda *a, **kw: [])
        candidates = [{"symbol": "1234.TW", "sector": "半導體業", "total_score": 80.0, "price": 100.0}]
        info_data = {"1234.TW": {"name": "測試股"}}
        price_data = {"1234.TW": _make_df(n=40)}
        result = ranker.rank_candidates(
            candidates, price_data, info_data, top_n=3,
            market_context={"regime": "BULL_TREND"}, market_date="2026-01-20",
            use_ai_cache=False,
        )
        assert len(result) == 1
        assert result[0]["is_fallback"] is True

    def test_successful_ai_ranking(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ranker, "DEEPSEEK_API_KEY", "fake-key")
        monkeypatch.setattr(ranker, "_CACHE_DIR", tmp_path)
        fake_response = [
            {"rank": 1, "ticker": "1234.TW", "reason": "測試理由", "risk": "測試風險",
             "confidence": 8, "buy_zone": "NT$95～NT$100", "target": "NT$120",
             "stop_loss": "NT$90", "hold_period": "10", "strategy": "動能策略",
             "strategy_reason": "RSI=62", "confidence_reason": "結構健康"},
        ]
        monkeypatch.setattr(ranker, "_call_deepseek", lambda *a, **kw: fake_response)
        candidates = [{"symbol": "1234.TW", "sector": "半導體業", "total_score": 80.0, "price": 100.0}]
        info_data = {"1234.TW": {"name": "測試股", "sector": "半導體業"}}
        price_data = {"1234.TW": _make_df(n=40)}
        result = ranker.rank_candidates(
            candidates, price_data, info_data, top_n=3,
            market_context={"regime": "BULL_TREND"}, market_date="2026-01-20",
            use_ai_cache=False,
        )
        assert len(result) == 1
        assert result[0]["symbol"] == "1234.TW"
        assert result[0]["confidence"] == 8
        assert result[0]["buy_zone"] == "NT$95～NT$100"
        assert "is_fallback" not in result[0]

        # 快取應已寫入
        cache_path = ranker._ranked_cache_path("2026-01-20")
        assert cache_path.exists()

    def test_ai_cache_reused_without_calling_api(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ranker, "DEEPSEEK_API_KEY", "fake-key")
        monkeypatch.setattr(ranker, "_CACHE_DIR", tmp_path)
        cache_path = tmp_path / "ranked_20260120.json"
        cache_path.write_text(
            json.dumps([{"rank": 1, "symbol": "1234.TW", "confidence": 7}], ensure_ascii=False),
            encoding="utf-8",
        )
        called = {"n": 0}
        monkeypatch.setattr(ranker, "_call_deepseek", lambda *a, **kw: called.__setitem__("n", called["n"] + 1) or [])
        candidates = [{"symbol": "1234.TW", "sector": "半導體業", "total_score": 80.0, "price": 100.0}]
        result = ranker.rank_candidates(
            candidates, {"1234.TW": _make_df(n=5)}, {}, top_n=3,
            market_context={"regime": "BULL_TREND"}, market_date="2026-01-20",
            use_ai_cache=True,
        )
        assert called["n"] == 0
        assert len(result) == 1
        assert result[0]["confidence"] == 7

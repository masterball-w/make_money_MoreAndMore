"""策略切换与共享组合单测 —— 全部 tmp_path 隔离，不碰真实数据。

覆盖：默认策略 / 切换落盘 / 非法名称 / 历史上限 / 坏文件回退 /
旧趋势状态迁移数学 / 迁移幂等 / 缺字段补齐 / 存取往返。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pytest

from common.strategy_switch import (DEFAULT_STRATEGY, STRATEGIES, default_portfolio,
                                    load_portfolio, load_strategy, load_strategy_state,
                                    save_portfolio, set_strategy, strategy_label)


class TestStrategyState:
    def test_default_hybrid_when_missing(self, tmp_path):
        p = tmp_path / "strategy_state.json"
        assert load_strategy(p) == "hybrid"
        st = load_strategy_state(p)
        assert st["source"] == "default" and st["history"] == []

    def test_switch_roundtrip(self, tmp_path):
        p = tmp_path / "strategy_state.json"
        st = set_strategy("spec", source="feishu", path=p)
        assert st["strategy"] == "spec" and st["source"] == "feishu"
        assert load_strategy(p) == "spec"
        assert len(st["history"]) == 1
        set_strategy("dca", source="h5", path=p)
        st = load_strategy_state(p)
        assert st["strategy"] == "dca" and st["source"] == "h5"
        assert len(st["history"]) == 2

    def test_invalid_name_raises(self, tmp_path):
        with pytest.raises(ValueError):
            set_strategy("yolo", path=tmp_path / "s.json")

    def test_history_cap_20(self, tmp_path):
        p = tmp_path / "s.json"
        for i in range(25):
            set_strategy("spec" if i % 2 else "dca", path=p)
        assert len(load_strategy_state(p)["history"]) == 20

    def test_corrupt_file_falls_back(self, tmp_path):
        p = tmp_path / "s.json"
        p.write_text("{not json", encoding="utf-8")
        assert load_strategy(p) == DEFAULT_STRATEGY

    def test_unknown_strategy_in_file_falls_back(self, tmp_path):
        p = tmp_path / "s.json"
        p.write_text(json.dumps({"strategy": "yolo"}), encoding="utf-8")
        assert load_strategy(p) == DEFAULT_STRATEGY

    def test_labels(self):
        assert strategy_label("dca") == "稳健长期定投"
        assert strategy_label("spec") == "短期投机"
        assert strategy_label("hybrid") == "混合并行（定投+事件投机）"
        assert set(STRATEGIES) == {"dca", "spec", "hybrid"}

    def test_switch_to_hybrid(self, tmp_path):
        p = tmp_path / "s.json"
        st = set_strategy("hybrid", source="h5", path=p)
        assert st["strategy"] == "hybrid" and load_strategy(p) == "hybrid"


class TestSharedLock:
    def test_lock_acquirable(self):
        from common.strategy_switch import acquire_portfolio_lock
        # Windows 无 fcntl 降级为无锁（返回真值）；Linux 返回文件句柄
        assert acquire_portfolio_lock(blocking=True)
        assert acquire_portfolio_lock(blocking=False)

    def test_default_portfolio_has_dip_drop(self):
        pf = default_portfolio()
        assert pf["dca"]["last_dip_drop"] == 0.0


class TestPortfolioMigration:
    def _trend(self, tmp_path):
        tp = tmp_path / "trend_state.json"
        tp.write_text(json.dumps({
            "cash": 50000.0, "qty": 65.3148, "avg_price": 765.523074,
            "last_price": 739.95, "high_water": 100443.27,
            "last_signal": True, "last_candle": "2026-11-16"}), encoding="utf-8")
        return tp

    def test_migration_math(self, tmp_path):
        tp = self._trend(tmp_path)
        pp = tmp_path / "portfolio_state.json"
        pf = load_portfolio(pp, trend_path=tp)
        assert pf["cash"] == 50000.0
        spy = pf["positions"]["SPY"]
        assert spy["qty"] == 65.3148 and spy["avg_price"] == 765.523074
        assert spy["trail_high"] == max(765.523074, 739.95), "跟踪高点取均价与现价的较大者"
        assert spy["tp_stage"] == 0
        assert pf["high_water"] == 100443.27
        assert pp.exists(), "迁移必须落盘（一次性）"

    def test_migration_idempotent(self, tmp_path):
        tp = self._trend(tmp_path)
        pp = tmp_path / "portfolio_state.json"
        pf = load_portfolio(pp, trend_path=tp)
        pf["cash"] = 12345.0
        save_portfolio(pf, pp)
        # 旧状态文件此后变化，不得影响已迁移的组合
        tp.write_text(json.dumps({"cash": 1.0, "qty": 0}), encoding="utf-8")
        again = load_portfolio(pp, trend_path=tp)
        assert again["cash"] == 12345.0
        assert again["positions"]["SPY"]["qty"] == 65.3148

    def test_no_trend_file_defaults(self, tmp_path):
        pf = load_portfolio(tmp_path / "pf.json", trend_path=tmp_path / "nope.json")
        assert pf["cash"] == 100_000.0 and pf["positions"] == {}

    def test_partial_file_backfilled(self, tmp_path):
        pp = tmp_path / "pf.json"
        pp.write_text(json.dumps({"cash": 8000.0}), encoding="utf-8")
        pf = load_portfolio(pp, trend_path=tmp_path / "nope.json")
        assert pf["cash"] == 8000.0
        assert pf["initial_cash"] == 100_000.0
        assert "last_candle" in pf["dca"] and "seen" in pf["spec"]

    def test_save_load_roundtrip(self, tmp_path):
        pp = tmp_path / "pf.json"
        pf = default_portfolio()
        pf["positions"]["NVDA"] = {"qty": 10.0, "avg_price": 150.0,
                                   "trail_high": 150.0, "tp_stage": 0}
        save_portfolio(pf, pp)
        assert load_portfolio(pp, trend_path=tmp_path / "nope.json") == pf

"""risk_limits.yaml 必须覆盖代码默认值，否则改配置文件不会影响实盘进程。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from decision_layer.runtime_config import load_runtime_config

ROOT = Path(__file__).resolve().parent.parent


def test_repo_config_matches_intraday_defaults():
    limits, strategy, breakout, flow = load_runtime_config(ROOT / "config" / "risk_limits.yaml")
    assert limits.max_risk_per_trade == 0.01
    assert limits.cooldown_minutes == 10
    # 2026-10-21 九天回测（V0-V3 对比）后的定版参数
    assert strategy.stop_loss_pct == 0.007
    assert strategy.take_profit_pct == 0.010
    assert strategy.time_stop_minutes == 20
    assert strategy.trend_filter is True
    assert strategy.follow_flow is True
    assert strategy.follow_min_strength == 0.45
    assert strategy.w_breakout == 0.55
    assert strategy.signal_ttl_minutes == 15
    assert strategy.min_decision_interval == 5.0
    assert strategy.breakeven_at == 0.004
    assert strategy.breakeven_lock == 0.0015
    assert strategy.trail_at == 0.007
    assert strategy.trail_gap == 0.0025
    assert strategy.mr_enable is False          # 震荡收割被回测否决，保持关闭
    assert strategy.maker_entries is True       # 挂单进场（回测 -160 → -81）
    assert breakout.donchian_period == 20
    assert flow.direction_z == 2.0


def test_yaml_override_and_legacy_ttl_key(tmp_path: Path):
    path = tmp_path / "risk_limits.yaml"
    path.write_text(
        "risk_limits:\n"
        "  cooldown_minutes: 3\n"
        "strategy:\n"
        "  stop_loss_pct: 0.02\n"
        "  take_profit_pct: null\n"
        "  breakout_ttl_minutes: 8\n"
        "breakout:\n"
        "  min_strength: 0.4\n"
        "flow:\n"
        "  window: 12\n",
        encoding="utf-8",
    )
    limits, strategy, breakout, flow = load_runtime_config(path)
    assert limits.cooldown_minutes == 3
    assert limits.max_position_pct == 0.20          # 未写的字段保留默认
    assert strategy.stop_loss_pct == 0.02
    assert strategy.take_profit_pct is None
    assert strategy.signal_ttl_minutes == 8         # 旧键名仍可读
    assert breakout.min_strength == 0.4
    assert flow.window == 12

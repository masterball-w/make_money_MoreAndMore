"""策略分发入口 —— systemd 定时器统一入口，按当前策略路由到对应引擎。

data/strategy_state.json 由飞书指令 / H5 按钮写入（common.strategy_switch）。
默认稳健长期定投（dca）；短期投机（spec）另有 20 分钟定时器，
非当前策略的引擎被触发时自行跳过，互不干扰。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from common.strategy_switch import load_strategy, strategy_label  # noqa: E402


def main() -> int:
    name = load_strategy()
    print(f"[策略分发] 当前策略: {strategy_label(name)} ({name})")
    if name == "spec":
        import spec_engine
        return spec_engine.main()
    import dca_engine
    return dca_engine.main()


if __name__ == "__main__":
    sys.exit(main())

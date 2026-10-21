"""版本信息 —— 部署时写入 config/version.yaml，启动推送与机器人查询共用。

文件格式（部署脚本/人工在每次发版时更新，随 git 提交）：
    version: "2478b68"
    date: "2026-10-21 12:30"
    changes:
      - "maker挂单进场（回测 -160→-81）"
      - "..."
缺文件时返回"未知版本"，不影响任何主流程。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_version() -> dict:
    path = ROOT / "config" / "version.yaml"
    if not path.exists():
        return {"version": "未知", "date": "", "changes": []}
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return {
            "version": str(data.get("version") or "未知"),
            "date": str(data.get("date") or ""),
            "changes": [str(c) for c in (data.get("changes") or [])][:8],
        }
    except Exception:  # noqa: BLE001 —— 版本信息坏了不影响交易
        return {"version": "未知", "date": "", "changes": []}


def version_markdown() -> str:
    """飞书卡片用的版本信息 markdown。"""
    v = load_version()
    lines = [f"🏷 **当前版本** `{v['version']}`"
             + (f"（{v['date']} 上线）" if v["date"] else "")]
    if v["changes"]:
        lines.append("本次更新：")
        lines.extend(f"• {c}" for c in v["changes"])
    else:
        lines.append("（未登记变更清单）")
    return "\n".join(lines)

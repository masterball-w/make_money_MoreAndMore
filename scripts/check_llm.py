"""LLM 连通性自检 —— 接入/更换 AI 分析 API 后先跑这个，30 秒确认全链路可用。

检查项（与决策层 LLMAnalyzer 真实调用路径一致）：
  1. 配置解析：settings.yaml llm 段 + 环境变量覆盖，密钥来源明示（脱敏显示）
  2. 基础对话：能否拿到非空回复
  3. 结构化输出：系统同款分析师提示词，返回的 JSON 能否按系统逻辑解析

用法:
    .venv/Scripts/python.exe scripts/check_llm.py
退出码: 0 全部通过（可放心启动 real_run.py）；1 存在失败项（系统会降级关键词模式）。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from common.llm_config import load_llm_config  # noqa: E402
from decision_layer.llm_analyzer import _SYSTEM_PROMPT, _strip_code_fence  # noqa: E402


def _mask(key: str | None) -> str:
    if not key:
        return "(未配置)"
    return f"{key[:8]}...{key[-4:]} (len={len(key)})"


async def main() -> int:
    cfg = load_llm_config(ROOT / "config" / "settings.yaml")
    print("=" * 60)
    print("LLM 连通性自检")
    print("=" * 60)
    print(f"  base_url : {cfg.base_url}")
    print(f"  model    : {cfg.model}")
    print(f"  timeout  : {cfg.timeout}s")
    print(f"  extra    : {cfg.extra_body or '(无)'}")
    print(f"  api_key  : {_mask(cfg.api_key)}")

    if not cfg.available:
        print("\n[1/3] 配置解析 ......... 失败：未找到密钥，系统将以关键词模式运行")
        print("\n结论: 不可用。请在 config/settings.yaml 填 api_key / api_key_file，")
        print("      或设置环境变量 OPENAI_API_KEY。")
        return 1
    print("[1/3] 配置解析 ......... 通过")

    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=cfg.api_key, base_url=cfg.base_url, timeout=cfg.timeout)
    extra = {"extra_body": cfg.extra_body} if cfg.extra_body else {}

    # ---- 基础对话 ----
    try:
        # GLM-5.3 即使关思考也残留少量推理 token，max_tokens 过小会截断正文，
        # 因此留足余量（生产路径 LLMAnalyzer 不设 max_tokens，无此问题）
        resp = await client.chat.completions.create(
            model=cfg.model,
            messages=[{"role": "user", "content": "只需回复两个字母: OK"}],
            max_tokens=256,
            **extra,
        )
        text = (resp.choices[0].message.content or "").strip()
        assert text, "返回内容为空"
        print(f"[2/3] 基础对话 ......... 通过 (回复: {text[:40]!r})")
    except Exception as e:  # noqa: BLE001
        print(f"[2/3] 基础对话 ......... 失败: {e}")
        print("\n结论: 端点/密钥/模型不可用，请核对 settings.yaml 的 llm 段。")
        return 1

    # ---- 结构化输出（系统同款提示词）----
    try:
        news = ("标题: 美联储宣布降息50个基点\n"
                "正文: 联邦公开市场委员会决定将联邦基金利率下调50基点，释放宽松信号。")
        resp = await client.chat.completions.create(
            model=cfg.model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": news},
            ],
            temperature=0.2,
            max_tokens=512,
            **extra,
        )
        raw = resp.choices[0].message.content or ""
        data = json.loads(_strip_code_fence(raw))
        for field_name in ("sentiment", "symbols", "horizon_minutes", "confidence", "reasoning"):
            assert field_name in data, f"缺少字段 {field_name}"
        print(f"[3/3] 结构化输出 ....... 通过 "
              f"(sentiment={data['sentiment']}, confidence={data['confidence']})")
        print(f"      reasoning: {str(data['reasoning'])[:60]}")
    except Exception as e:  # noqa: BLE001
        print(f"[3/3] 结构化输出 ....... 失败: {e}")
        print("\n结论: 模型可用但不按系统要求输出 JSON，建议换模型")
        print("      （已验证 glm-4.5-flash / glm-4-flash-250414 可用）。")
        return 1

    print("\n结论: 全部通过，real_run.py 将以 LLM 模式做新闻分析。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

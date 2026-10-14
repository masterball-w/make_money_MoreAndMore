"""LLM 接入配置加载器 —— 金融软件与 AI 分析 API 之间的唯一配置入口。

加载 config/settings.yaml 的 llm 段，产出 LLMAnalyzer 可直接使用的 LLMConfig。
任何 OpenAI 兼容服务（智谱 GLM / OpenAI / DeepSeek / 自建网关）只改 base_url + model
两个即可接入，教程见 docs/LLM接入教程.md。

取值优先级（高 → 低）：
  base_url / model : 环境变量 OPENAI_BASE_URL / LLM_MODEL > settings.yaml > 默认值
  api_key          : settings.yaml 的 api_key > api_key_file > 环境变量 OPENAI_API_KEY
  密钥文件不进版本库（.gitignore 已排除 config/api.txt）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_DEFAULTS = {
    # 编程套餐端点走 Coding Plan 额度；通用端点 /api/paas/v4 走账户充值余额
    "base_url": "https://open.bigmodel.cn/api/coding/paas/v4/",
    "model": "glm-5.3",
    "timeout_seconds": 20.0,
}


@dataclass
class LLMConfig:
    """一次 LLM 接入所需的全部参数。"""

    api_key: str | None = None
    base_url: str = _DEFAULTS["base_url"]
    model: str = _DEFAULTS["model"]
    timeout: float = _DEFAULTS["timeout_seconds"]
    # 供应商私有参数，随请求透传（如智谱 thinking 开关）；None 表示不传
    extra_body: dict | None = field(default=None)

    @property
    def available(self) -> bool:
        """是否具备真实调用条件（无 key 则系统降级关键词模式）。"""
        return bool(self.api_key)


def load_llm_config(settings_path: Path | None = None) -> LLMConfig:
    """读取 settings.yaml llm 段并应用环境变量覆盖，返回完整 LLM 配置。

    settings.yaml 缺失或解析失败时静默回退到环境变量 + 默认值，
    保证调用方永远拿到一个可用的 LLMConfig（系统降级原则）。
    """
    data: dict = {}
    path = Path(settings_path) if settings_path else ROOT / "config" / "settings.yaml"
    if path.exists():
        try:
            import yaml  # 可选依赖，核心链路不强制

            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception:  # noqa: BLE001 —— 配置坏文件不阻断启动
            data = {}
    llm = data.get("llm") or {}

    # ---- 密钥：yaml 明文 > 密钥文件 > 环境变量 ----
    api_key = str(llm.get("api_key") or "").strip()
    if not api_key:
        key_file = str(llm.get("api_key_file") or "").strip()
        if key_file:
            kf = ROOT / key_file
            if kf.exists():
                api_key = kf.read_text(encoding="utf-8").strip()
    if not api_key:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()

    # ---- 端点与模型：环境变量 > yaml > 默认值 ----
    base_url = (os.environ.get("OPENAI_BASE_URL") or llm.get("base_url")
                or _DEFAULTS["base_url"]).strip()
    model = (os.environ.get("LLM_MODEL") or llm.get("model")
             or _DEFAULTS["model"]).strip()
    timeout = float(llm.get("timeout_seconds") or _DEFAULTS["timeout_seconds"])

    # ---- 供应商私有参数：thinking 开关（仅 GLM 4.5+ 生效，其他端点自动忽略）----
    extra_body: dict | None = None
    thinking = str(llm.get("thinking") or "").strip().lower()
    if thinking in ("disabled", "enabled"):
        extra_body = {"thinking": {"type": thinking}}

    return LLMConfig(
        api_key=api_key or None,
        base_url=base_url,
        model=model,
        timeout=timeout,
        extra_body=extra_body,
    )

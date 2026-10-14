"""飞书自建应用机器人 —— 长连接模式，群里 @机器人 即问即答。

与自定义机器人（只能 webhook 发送）不同，自建应用可通过开放平台
"长连接"事件订阅接收群消息，无需公网回调地址。

依赖：lark-oapi（服务器已装：pip install lark-oapi）
配置：config/feishu_app.txt 两行  app_id:xxx  /  app_secret:xxx
      （缺文件时本服务静默退出，systemd 用 ConditionPathExists 门控）

回复卡片：指令摘要（状态/持仓/帮助）+ 「打开监控台」按钮（H5 页面）。
前置：应用需开通 机器人能力 + im:message 权限 + 订阅 im.message.receive_v1
     （订阅方式选"长连接"），并发布版本、把机器人拉进群。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

import os

import lark_oapi as lark                                    # noqa: E402
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1        # noqa: E402

from common.notifier import load_notifier                   # noqa: E402
from feishu_bot_server import _dispatch, _load_callback_token  # noqa: E402

APP_FILE = ROOT / "config" / "feishu_app.txt"


def _load_base_url() -> str:
    """监控台对外地址：优先环境变量 TRADING_BASE_URL，其次 notify.yaml 的 base_url。"""
    url = os.environ.get("TRADING_BASE_URL", "").strip()
    if not url:
        try:
            import yaml

            cfg = yaml.safe_load((ROOT / "config" / "notify.yaml").read_text(encoding="utf-8")) or {}
            url = str(cfg.get("base_url") or "").strip()
        except Exception:  # noqa: BLE001
            url = ""
    return url.rstrip("/")

COMMANDS = {"状态", "快照", "持仓", "帮助", "help", "在吗"}


def _load_app_creds() -> tuple[str, str]:
    creds: dict[str, str] = {}
    for line in APP_FILE.read_text(encoding="utf-8").splitlines():
        if ":" in line:
            k, v = line.strip().split(":", 1)
            creds[k.strip().lower()] = v.strip()
    return creds["app_id"], creds["app_secret"]


def _extract_text(data: P2ImMessageReceiveV1) -> str:
    try:
        content = json.loads(data.event.message.content or "{}")
        return str(content.get("text") or "")
    except Exception:  # noqa: BLE001
        return ""


def build_client(app_id: str, app_secret: str) -> lark.Client:
    return lark.Client.builder().app_id(app_id).app_secret(app_secret).build()


def _mention_open_id(m) -> str:  # noqa: ANN001
    """mention 的 id 可能是字符串，也可能是含 open_id 属性的 UserId 对象。"""
    raw = getattr(m, "id", "")
    if isinstance(raw, str):
        return raw
    return str(getattr(raw, "open_id", "") or getattr(raw, "union_id", "") or "")


def make_handler(client: lark.Client, page_url: str, bot_open_id: str = "",
                 bot_name: str = ""):  # noqa: ANN201
    def on_message(data: P2ImMessageReceiveV1) -> None:
        msg = data.event.message
        # 调试日志：事件到达即记录（排查"被@不回复"类问题）
        try:
            m_debug = [(str(getattr(m, "key", "")), str(getattr(m, "id", "")),
                        str(getattr(m, "name", ""))) for m in (msg.mentions or [])]
            print(f"[app-bot] debug 事件: chat_type={msg.chat_type} "
                  f"mentions={m_debug} 我的id={bot_open_id[:14] or '?'}")
        except Exception as e:  # noqa: BLE001
            print(f"[app-bot] debug 日志异常: {e}")
        if str(msg.chat_type) != "group":        # 只响应群聊
            return
        text = _extract_text(data)
        # 触发条件（满足其一）：
        #   1. 真正的 @我（mention id 精确匹配，兼容字符串/UserId 对象两种形态）
        #   2. 消息文本里叫我的名字（兼容"纯文本@"没选成员列表的情况）
        mentions = list(msg.mentions or [])
        if bot_open_id:
            at_me = any(_mention_open_id(m) == bot_open_id for m in mentions)
        else:
            at_me = bool(mentions)
        called_by_name = bool(bot_name) and bot_name in text
        if not (at_me or called_by_name):
            return
        print(f"[app-bot] 触发(@我={at_me} 叫名={called_by_name}): {text[:60]!r}")
        summary = _dispatch(text)
        card = {
            "config": {"wide_screen_mode": True},
            "elements": [
                {"tag": "markdown", "content": summary},
                {"tag": "hr"},
                {"tag": "action", "actions": [
                    {"tag": "button",
                     "text": {"tag": "plain_text", "content": "📊 打开实时监控台"},
                     "type": "primary", "url": page_url},
                ]},
            ],
        }
        # 回复通道：走自定义机器人 webhook（app API 的 tenant token 可能被
        # 平台访问控制策略拒绝——错误 21001——webhook 不受影响）
        ok = load_notifier().send_feishu_card(card)
        print(f"[app-bot] 回复(webhook): {'ok' if ok else '失败'}")

    return on_message


def _fetch_bot_identity(app_id: str, app_secret: str) -> tuple[str, str]:
    """取机器人自身 open_id 与名称（带重试；lark-oapi 1.7.4 未封装此接口，走原生 HTTP）。"""
    import time

    import httpx

    for attempt in range(3):
        try:
            r = httpx.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret}, timeout=15)
            token = str(r.json().get("tenant_access_token") or "")
            if not token:
                raise RuntimeError(f"token 接口返回异常: {str(r.text)[:120]}")
            r2 = httpx.get("https://open.feishu.cn/open-apis/bot/v3/info/",
                           headers={"Authorization": f"Bearer {token}"}, timeout=15)
            info = r2.json().get("bot") or r2.json().get("bot_info") or {}
            open_id = str(info.get("open_id") or "")
            if open_id:
                return open_id, str(info.get("app_name") or "")
            raise RuntimeError(f"bot info 无 open_id: {str(r2.text)[:120]}")
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                print(f"[app-bot] 身份获取失败(重试{attempt+1}次): {e}")
                return "", ""
            time.sleep(2)
    return "", ""


def main() -> None:
    app_id, app_secret = _load_app_creds()
    token = _load_callback_token()
    base_url = _load_base_url()
    if not base_url:
        print("[app-bot] 未配置监控台地址（TRADING_BASE_URL 或 notify.yaml 的 base_url），按钮将不可用")
    page_url = f"{base_url}/app/{token}"
    client = build_client(app_id, app_secret)
    # 拿机器人自身 open_id + 名称，用于精确判断"被@"（带重试）
    bot_open_id, bot_name = _fetch_bot_identity(app_id, app_secret)
    if bot_open_id:
        print(f"[app-bot] 机器人身份确认: {bot_open_id[:16]}... 名称: {bot_name}")
    else:
        print("[app-bot] 未取到机器人身份（退化为任意@都响应）")
    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(
                   make_handler(client, page_url, bot_open_id, bot_name))
               .build())
    print(f"[app-bot] 长连接启动，响应@我和叫我名字的消息；监控台按钮指向 {base_url or '(未配置)'}/app/{token[:8]}***")
    ws = lark.ws.Client(app_id, app_secret,
                        event_handler=handler, log_level=lark.LogLevel.INFO)
    ws.start()


if __name__ == "__main__":
    main()

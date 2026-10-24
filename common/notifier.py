"""手机推送客户端 —— 多通道（飞书自定义机器人 / 企业微信群机器人）。

用于两类推送：
  1. 实时事件告警（成交回报/进程启停），由 real_run.py 事件桥调用；
  2. 每日日报，由 scripts/daily_report.py 定时任务调用。

配置在 config/notify.yaml（已 gitignore，含 webhook 地址）：
    channel: feishu                       # feishu | wecom
    feishu_webhook: https://open.feishu.cn/open-apis/bot/v2/hook/xxx
    wecom_webhook: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
    cooldown_minutes: 10                  # 同标的告警冷却
    quiet_hours: []                       # 免打扰时段 [起, 止]（小时），日报不受限

未配置时所有发送静默跳过（系统降级原则：通知故障绝不影响交易主链路）。
飞书机器人限频 100 条/分钟；企业微信 20 条/分钟。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

import httpx

logger = logging.getLogger("notifier")

ROOT = Path(__file__).resolve().parent.parent


class WeComNotifier:
    """多通道推送器：按 channel 路由到飞书或企业微信 webhook。"""

    def __init__(self, config_path: Path | None = None, timeout: float = 10.0) -> None:
        self.channel = ""
        self.feishu_webhook = ""
        self.wecom_webhook = ""
        self.cooldown = 600.0
        self.quiet_hours: list = []
        self._timeout = timeout
        self._last_sent: dict[str, float] = {}      # 冷却计时: key -> ts

        config_path = config_path or ROOT / "config" / "notify.yaml"
        if config_path.exists():
            try:
                import yaml

                with open(config_path, encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                self.channel = str(data.get("channel") or "").strip().lower()
                self.feishu_webhook = str(data.get("feishu_webhook") or "").strip()
                self.wecom_webhook = str(data.get("wecom_webhook") or "").strip()
                self.cooldown = float(data.get("cooldown_minutes", 10)) * 60
                self.quiet_hours = data.get("quiet_hours") or []
            except Exception:  # noqa: BLE001 —— 配置异常不阻断交易主链路
                logger.warning("notify.yaml 解析失败，通知功能停用")
        if not self.channel:                        # 按已有的 webhook 自动推断通道
            if self.feishu_webhook:
                self.channel = "feishu"
            elif self.wecom_webhook:
                self.channel = "wecom"

    @property
    def available(self) -> bool:
        return (self.channel == "feishu" and bool(self.feishu_webhook)) or \
               (self.channel == "wecom" and bool(self.wecom_webhook))

    # ------------------------------------------------------------ 通道适配

    def _post_feishu(self, markdown: str) -> bool:
        """飞书自定义机器人：interactive 卡片 + markdown 元素（支持加粗/列表）。"""
        payload = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "elements": [{"tag": "markdown", "content": markdown[:3800]}],
            },
        }
        resp = httpx.post(self.feishu_webhook, timeout=self._timeout, json=payload)
        data = resp.json()
        ok = data.get("code") == 0 or data.get("StatusCode") == 0
        if not ok:
            logger.warning("飞书推送被拒: %s", str(data)[:200])
        return ok

    def _post_wecom(self, markdown: str) -> bool:
        """企业微信群机器人：markdown 消息（4096 字节限制）。"""
        resp = httpx.post(self.wecom_webhook, timeout=self._timeout,
                          json={"msgtype": "markdown",
                                "markdown": {"content": markdown[:3800]}})
        data = resp.json()
        ok = data.get("errcode") == 0
        if not ok:
            logger.warning("企业微信推送被拒: %s", str(data)[:200])
        return ok

    def _in_quiet_hours(self) -> bool:
        if not (isinstance(self.quiet_hours, (list, tuple)) and len(self.quiet_hours) == 2):
            return False
        now = datetime.now().hour + datetime.now().minute / 60
        lo, hi = float(self.quiet_hours[0]), float(self.quiet_hours[1])
        return lo <= now <= hi if lo <= hi else (now >= lo or now <= hi)

    def send_feishu_card(self, card: dict) -> bool:
        """直接发送完整飞书 interactive 卡片（含按钮等复杂元素），走 webhook 通道。

        用于 app 机器人回复（app API 令牌被平台策略限制时的替代发言通道）。
        """
        if self.channel != "feishu" or not self.feishu_webhook:
            logger.info("飞书通道未配置，跳过卡片发送")
            return False
        try:
            resp = httpx.post(self.feishu_webhook, timeout=self._timeout,
                              json={"msg_type": "interactive", "card": card})
            data = resp.json()
            ok = data.get("code") == 0 or data.get("StatusCode") == 0
            if not ok:
                logger.warning("飞书卡片被拒: %s", str(data)[:200])
            return ok
        except Exception as e:  # noqa: BLE001
            logger.warning("飞书卡片发送失败: %s", e)
            return False

    def send_markdown(self, content: str, dedup_key: str = "") -> bool:
        """推送消息。dedup_key 非空时应用冷却窗口（同 key N 分钟一条）。

        任何失败只记日志返回 False，绝不抛异常影响交易进程。
        """
        if not self.available:
            logger.info("通知未配置，跳过: %s", content.splitlines()[0][:50])
            return False
        if dedup_key:
            now = time.monotonic()
            if now - self._last_sent.get(dedup_key, -1e9) < self.cooldown:
                return False
            self._last_sent[dedup_key] = now
        try:
            if self.channel == "feishu":
                return self._post_feishu(content)
            return self._post_wecom(content)
        except Exception as e:  # noqa: BLE001
            logger.warning("推送失败(%s): %s", self.channel, e)
            return False

    def should_mute(self) -> bool:
        """当前是否处于免打扰时段（供定时快照等非紧急推送自查；P1 成交不受限）。"""
        return self._in_quiet_hours()

    # ------------------------------------------------------------ 业务封装

    def notify_fill(self, symbol: str, side: str, qty: float, price: float,
                    commission: float) -> bool:
        """成交回报（唯一 P1 级事件，不受免打扰时段限制）。"""
        coin = {"BTC": "比特币", "ETH": "以太坊", "SPY": "标普500ETF"}.get(symbol.split("/")[0],
                                                    symbol.split("/")[0])
        buying = "BUY" in side.upper()
        value = qty * price
        if buying:
            icon, action = "🟢", f"花了约 **{value:,.0f} USD**，买入 **{qty:.2f} 股{coin}**（单价 {price:,.2f}）"
        else:
            icon, action = "🔴", f"卖出 **{qty:.2f} 股{coin}**，收回约 **{value:,.0f} USD**（单价 {price:,.2f}）"
        return self.send_markdown(
            f"{icon} **机器人{'买入' if buying else '卖出'}啦**\n"
            f"{action}\n手续费 {commission:.2f} USD | {datetime.now():%H:%M:%S}",
            dedup_key=f"fill:{symbol}")

    def notify_lifecycle(self, event: str) -> bool:
        """监控进程启停通知（systemd 崩溃重启时手机能立刻知道）。"""
        if event == "启动":
            text = "🚀 **交易小助手已上岗**，开始盯盘"
        else:
            text = "🛑 **交易小助手暂时下线**，系统会自动尝试重新拉起"
        return self.send_markdown(f"{text}（{datetime.now():%m-%d %H:%M:%S}）",
                                  dedup_key=f"lifecycle:{event}")


def load_notifier() -> WeComNotifier:
    """交易进程内加载通知器（未配置时返回不可用实例，调用全部安全跳过）。"""
    return WeComNotifier()

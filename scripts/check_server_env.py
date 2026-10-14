"""服务器部署前置自检 —— 在目标服务器上跑一次，判断能否承载本交易系统。

检查项：
  1. Python 版本（需 >= 3.11）
  2. 系统资源（CPU / 内存 / 磁盘，本系统 1核2G 即可）
  3. 数据源网络可达性（OKX/Binance K线、Google News/Fed/Cointelegraph RSS、智谱 LLM 端点）
     —— 大陆机房对部分境外数据源可能不通，结果决定部署形态
  4. LLM 密钥连通（可选，需已放置 config/api.txt）

用法（在服务器项目目录内）:
    python scripts/check_server_env.py
退出码: 0 = 检查完成（结果看汇总表，是否可部署由数据源决定）
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

import httpx  # venv 内已装（服务器上先完成 pip install）

# 数据源清单: (名称, URL, 影响, 额外可接受的响应码)
SOURCES = [
    ("OKX K线(主源)", "https://www.okx.com/api/v5/market/candles?instId=BTC-USDT&bar=1m&limit=3",
     "行情主源，不通则看 Binance 备源", ()),
    ("OKX 资金流", "https://www.okx.com/api/v5/rubik/stat/taker-volume?ccy=BTC&instType=SPOT",
     "资金流异常检测（F1~F3）", ()),
    ("Binance K线(备源)", "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=3",
     "故障切换备源，不通则主备切换失效", ()),
    ("GateIO K线", "https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair=BTC_USDT&interval=1m&limit=3",
     "大陆机房可达的行情源（--exchanges gateio）", ()),
    ("Google News RSS", "https://news.google.com/rss/search?q=%E7%BE%8E%E8%81%94%E5%82%A8&hl=zh-CN",
     "6 路关键词新闻订阅（信息层主源之一）", ()),
    ("美联储 RSS", "https://www.federalreserve.gov/feeds/press_monetary.xml",
     "FOMC 一手声明（P1 级新闻）", ()),
    ("Cointelegraph RSS", "https://cointelegraph.com/rss",
     "加密行业新闻", ()),
    ("智谱 LLM 端点", "https://open.bigmodel.cn/api/coding/paas/v4/models",
     "GLM-5.3 新闻分析（大陆机房通常畅通）", (401,)),  # 401=端点可达，仅缺鉴权头
]


def check_python() -> bool:
    ok = sys.version_info >= (3, 11)
    print(f"  Python       : {sys.version.split()[0]} {'✅' if ok else '❌ 需 >= 3.11'}")
    return ok


def check_resources() -> None:
    print(f"  CPU 核数     : {os_cpu_count() or '?'}（本系统 1 核即可）")
    try:
        if sys.platform == "linux":
            mem = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                k, v = line.split(":", 1)
                mem[k] = int(v.strip().split()[0])
            total_gb = mem.get("MemTotal", 0) / 1024 / 1024
            avail_gb = mem.get("MemAvailable", 0) / 1024 / 1024
            print(f"  内存         : 总 {total_gb:.1f}G / 可用 {avail_gb:.1f}G（本系统 2G 即可）")
        else:
            print("  内存         : 非 Linux，跳过（需求仅 2G）")
    except Exception:
        print("  内存         : 读取失败（需求仅 2G，一般机器都满足）")
    free_gb = shutil.disk_usage(str(ROOT)).free / 1024**3
    print(f"  磁盘剩余     : {free_gb:.1f}G（依赖 + 审计库 < 1G）")


def os_cpu_count() -> int | None:
    try:
        return int(subprocess.run(["nproc"], capture_output=True, text=True,
                                  timeout=5).stdout.strip() or 0) or None
    except Exception:
        return None


def check_network() -> list[tuple[str, bool, str]]:
    print("\n[2/3] 数据源网络可达性（每项 10s 超时）")
    results: list[tuple[str, bool, str]] = []
    with httpx.Client(timeout=10.0, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0"}) as client:
        for name, url, impact, accept in SOURCES:
            try:
                r = client.get(url)
                ok = r.status_code < 400 or r.status_code in accept
                note = f"HTTP {r.status_code}"
            except Exception as e:  # noqa: BLE001
                ok, note = False, type(e).__name__
            mark = "✅" if ok else "❌"
            print(f"  {mark} {name:18s} {note:12s} | {impact}")
            results.append((name, ok, impact))
    return results


def check_llm() -> None:
    print("\n[3/3] LLM 密钥（可选）")
    key_file = ROOT / "config" / "api.txt"
    if not key_file.exists():
        print("  ⏭ config/api.txt 不存在，跳过（部署后放置密钥再跑 scripts/check_llm.py）")
        return
    key = key_file.read_text(encoding="utf-8").strip()
    try:
        r = httpx.get("https://open.bigmodel.cn/api/coding/paas/v4/models",
                      headers={"Authorization": f"Bearer {key}"}, timeout=15)
        print(f"  {'✅' if r.status_code == 200 else '❌'} 智谱密钥 HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ 请求失败: {type(e).__name__}")


def main() -> int:
    print("=" * 68)
    print("服务器部署前置自检")
    print("=" * 68)
    print("[1/3] 运行环境")
    py_ok = check_python()
    check_resources()
    results = check_network()
    check_llm()

    ok_set = {name for name, ok, _ in results}
    okx_ok = "OKX K线(主源)" in ok_set
    gateio_ok = "GateIO K线" in ok_set
    news_ok = {"Google News RSS", "美联储 RSS", "Cointelegraph RSS"} & ok_set

    print("\n" + "=" * 68)
    print("结论")
    print("=" * 68)
    if not py_ok:
        print("  ❌ Python 版本不足，先升级到 3.11+ 再部署。")
    if (okx_ok or gateio_ok) and len(news_ok) >= 1:
        print("  ✅ 核心链路可用（行情 + 至少一路新闻），可以直接部署。")
        if not okx_ok and gateio_ok:
            print("     OKX 不可达但 GateIO 可用：启动时加 --exchanges gateio。")
        if "Google News RSS" not in ok_set:
            print("     Google News 不可达：启动时加 --no-gnews（保留 Fed/Cointelegraph）。")
    elif okx_ok or gateio_ok:
        print("  ⚠️ 行情可用但新闻源全断：系统会跑但 LLM 分析没有输入，")
        print("     建议接入国内新闻源（财联社/新浪财经 RSS，README 阶段2 已预留）。")
    else:
        print("  ❌ OKX 与 GateIO 均不通：无法做加密行情，")
        print("     该服务器只适合跑 A 股（akshare 大陆畅通）或仅作监控面板。")
    print("  提示: Binance 备源单断不影响运行（主备切换会自动跳过）；")
    print("        全部数据源断 = 服务器在境外受限网络，建议换港/新节点。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

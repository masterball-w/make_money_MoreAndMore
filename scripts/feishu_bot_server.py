"""飞书群机器人消息回调服务 —— 在群里 @机器人 发指令，秒回实时信息。

工作流：群成员 @机器人 发消息 → 飞书 POST 到本服务（回调地址）
       → 解析指令 → 生成内容 → 通过机器人 webhook 回复到群里。

支持指令（模糊匹配，如"@交易播报 状态"）：
    策略 / 切换   → 当前策略与切换方法
    定投          → 切换到稳健长期定投
    投机          → 切换到短期投机
    状态 / 快照   → 实时快照（行情/近1h决策/健康度）
    持仓          → 模拟盘持仓（按审计成交记录重建）
    版本          → 当前策略版本
    帮助 / 其他    → 指令列表

Web 控制台（同端口）：
    GET /app/<token>            → 移动端 H5 页面（K线/盈亏/资金动向/策略切换）
    GET /api/<token>/overview   → 实时数据 JSON（页面轮询源）
    POST /api/<token>/strategy  → H5 策略切换（body: {"name": "dca"|"spec"}）
    GET /static/echarts.min.js  → 自托管图表库（大陆机房免 CDN）

部署：systemd feishu-bot.service，监听 127.0.0.1:8300；
     nginx 将 /feishu/ /poke/ /app/ /api/ /static/ 反代到本服务。
依赖：仅标准库 + 已装的 httpx/yaml（无新增依赖）。
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

import httpx  # noqa: E402

from common.notifier import load_notifier  # noqa: E402
from common.strategy_switch import (load_strategy, load_strategy_state,  # noqa: E402
                                    set_strategy, strategy_label)
from daily_report import _coin, _parse_ts as _parse_any, account_state, build_report  # noqa: E402
from decision_layer.audit import AuditLog  # noqa: E402

DB_PATH = ROOT / "data" / "audit.db"
DEFAULT_SYMBOL = "SPY"
PORT = 8300

_HELP = ("🤖 **我是你的美股理财小助手**，在群里 @我 说：\n"
         "**策略** —— 看看现在跑的是哪个策略，怎么切换\n"
         "**定投** —— 切换到稳健长期定投（周定投+逢低买入+止盈）\n"
         "**投机** —— 切换到短期投机（信息流+资金动向，七巨头）\n"
         "**混合** —— 定投+事件投机并行（SPY 底仓定投，同时抓七巨头利好/利空事件）\n"
         "**状态** —— 看看现在整体怎么样（资产/行情/我在想什么）\n"
         "**持仓** —— 现在手里有什么，赚了还是亏了\n"
         "**版本** —— 当前运行的策略版本和更新内容")


def _load_callback_token() -> str:
    """从 notify.yaml 读回调 token（不存在则生成并写回）。"""
    import secrets

    cfg_path = ROOT / "config" / "notify.yaml"
    token = ""
    if cfg_path.exists():
        try:
            import yaml

            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            token = str(data.get("callback_token") or "").strip()
        except Exception:  # noqa: BLE001
            token = ""
    if not token:
        token = secrets.token_urlsafe(24)
        try:
            with open(cfg_path, "a", encoding="utf-8") as f:
                f.write(f"\n# 回调地址防伪 token（feishu_bot_server 使用）\ncallback_token: \"{token}\"\n")
        except Exception as e:  # noqa: BLE001
            print(f"[bot] token 写入失败: {e}")
    return token


def _find_text(obj) -> str:
    """从回调 JSON 中递归找出第一条疑似消息文本（兼容不同事件格式）。"""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        for v in obj.values():
            t = _find_text(v)
            if t:
                return t
    if isinstance(obj, list):
        for v in obj:
            t = _find_text(v)
            if t:
                return t
    return ""


def _overview_payload() -> dict:
    """H5 页面与机器人卡片共用的实时数据视图。"""
    if not DB_PATH.exists():
        return {"error": "no data"}
    audit = AuditLog(str(DB_PATH))
    try:
        klines = sorted(audit.recent_klines(DEFAULT_SYMBOL, 180), key=lambda k: k["ts"])
        flows = audit.recent_flows(DEFAULT_SYMBOL, 45)
        decisions = audit.recent_decisions(10)
        fills = audit.recent_fills(6)
        acct = account_state(audit, DEFAULT_SYMBOL)
        price = acct["last_price"]
        # 日线节奏：以上一根收盘为涨跌基准（周末无新柱时 24h 查找会误取多月前旧柱）
        day_ago = float(klines[-2]["close"]) if len(klines) >= 2 else price
        k_slim = [{"ts": k["ts"], "o": float(k["open"]), "h": float(k["high"]),
                   "l": float(k["low"]), "c": float(k["close"]), "v": float(k["volume"])}
                  for k in klines]
        f_slim = [{"ts": f["ts"], "net": round(float(f["net_flow"]), 4),
                   "buy": round(float(f["buy_volume"]), 4),
                   "sell": round(float(f["sell_volume"]), 4)} for f in flows]
        d_slim = [{"ts": d["ts"], "action": str(d["action"]).upper().rsplit(".", 1)[-1],
                   "reason": str(d.get("reason") or "")[:80],
                   "conf": float(d.get("confidence") or 0)} for d in decisions]
        fill_slim = [{"ts": f["ts"], "side": "BUY" if "BUY" in str(f["side"]).upper() else "SELL",
                      "qty": float(f["filled_qty"]), "price": float(f["avg_price"])}
                     for f in fills]
        last_k = klines[-1]["ts"] if klines else ""
        strat = load_strategy()
        return {"symbol": DEFAULT_SYMBOL, "price": price,
                "strategy": strat, "strategy_label": strategy_label(strat),
                "chg24h": (price / day_ago - 1) * 100 if day_ago else 0.0,
                "account": {k: (round(v, 2) if isinstance(v, float) else v)
                            for k, v in acct.items() if k != "positions"},
                "positions": acct["positions"],
                "klines": k_slim, "flows": f_slim, "decisions": d_slim, "fills": fill_slim,
                "server_time": datetime.now().strftime("%m-%d %H:%M:%S"),
                "kline_age_min": round((datetime.now(timezone.utc)
                                        - _parse_any(last_k)).total_seconds() / 60, 1) if last_k else None}
    finally:
        audit.close()


def _positions_reply() -> str:
    """持仓查询 —— 用户友好表述（按审计成交记录重建）。"""
    if not DB_PATH.exists():
        return "📉 小助手还没有做过任何交易"
    audit = AuditLog(str(DB_PATH))
    try:
        acct = account_state(audit, DEFAULT_SYMBOL)
        coin = _coin(DEFAULT_SYMBOL)
        L = ["💼 **现在手里有什么**"]
        if acct["positions"]:
            for p in acct["positions"]:
                last = p["last_price"]
                word = (f"浮赚 **+{p['pnl_pct']:.2f}%**" if p["pnl_pct"] >= 0
                        else f"浮亏 **{p['pnl_pct']:.2f}%**")
                L.append(f"{p['qty']:.2f} 股{coin}（买入均价 {p['avg_price']:,.2f}"
                         + (f"，现在值 {p['qty'] * last:,.0f} USD，{word}" if last else "") + "）")
        else:
            L.append("现在是空仓（全是现金），在等合适的机会")
        pnl_word = (f"赚了 {acct['pnl']:+,.0f} USD" if acct["pnl"] >= 0
                    else f"亏了 {abs(acct['pnl']):,.0f} USD")
        L.append(f"总资产 **{acct['equity']:,.0f} USD**，开局至今{pnl_word}（{acct['pnl_pct']:+.2f}%）")
        L.append(f"累计交易：买入 {acct['n_buy']} 次 / 卖出 {acct['n_sell']} 次")
        return "\n".join(L)
    finally:
        audit.close()


def _snapshot_reply() -> str:
    if not DB_PATH.exists():
        return "📉 暂无数据（系统刚部署？）"
    audit = AuditLog(str(DB_PATH))
    try:
        return build_report(audit, DEFAULT_SYMBOL, 1,
                            title="⏱ **美股理财小助手 · 实时播报**")
    finally:
        audit.close()


def _strategy_reply() -> str:
    """当前策略查询（只读，不切换）。"""
    st = load_strategy_state()
    name = st["strategy"]
    L = [f"🧭 **当前策略: {strategy_label(name)}**（{name}）"]
    if st.get("updated_at"):
        L.append(f"最近切换: {st['updated_at'][:16].replace('T', ' ')} UTC"
                 f"（来源: {st.get('source') or 'default'}）")
    L.append("发 **定投** → 稳健长期定投：周定投+自动识别回调逢低买入+分批/跟踪止盈")
    L.append("发 **投机** → 短期投机：信息流+机构资金动向判断七巨头，重大利空提前抛售")
    L.append("发 **混合** → 定投+事件投机并行：SPY 底仓照常定投，同时抓七巨头利好/利空事件")
    L.append("也可在 H5 监控台一键切换；切换不清仓，SPY 底仓始终保留")
    return "\n".join(L)


def _switch_reply(name: str) -> str:
    """飞书指令切换策略（落盘 + 确认回复）。"""
    set_strategy(name, source="feishu")
    if name == "dca":
        return ("✅ 已切换到 **稳健长期定投**\n"
                "周定投 + 自动识别回调逢低买入 + 分批/跟踪止盈，SPY 底仓继续由定投引擎管理。\n"
                "发 **投机** 可切到短期投机，发 **策略** 查看当前状态")
    if name == "hybrid":
        return ("✅ 已切换到 **混合并行（定投+事件投机）**\n"
                "SPY 底仓照常周定投+逢低买入；同时新闻引擎并行跑七巨头，"
                "利好事件（高管回归/新模型/IPO）驱动买入，重大利空提前抛售。\n"
                "发 **定投** 或 **投机** 可切回单一策略")
    return ("✅ 已切换到 **短期投机**\n"
            "信息流 + 机构资金动向判断七巨头；重大利空（如核心高管离职）提前抛售持仓"
            "并进入重点观察期，按跌幅分级低吸。\n"
            "SPY 底仓已冻结（投机引擎永不交易 SPY），只用剩余现金。\n"
            "发 **定投** 可切回稳健长期定投")


def _dispatch(text: str) -> str:
    if "混合" in text:
        return _switch_reply("hybrid")
    if "定投" in text:
        return _switch_reply("dca")
    if "投机" in text:
        return _switch_reply("spec")
    if ("策略" in text) or ("切换" in text):
        return _strategy_reply()
    if "版本" in text:
        from common.version import version_markdown
        return version_markdown()
    if "持仓" in text:
        return _positions_reply()
    if ("状态" in text) or ("快照" in text):
        return _snapshot_reply()
    return _HELP


_HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>交易监控台</title>
<script src="/static/echarts.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{background:#0d1117;color:#e6edf3;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;padding:12px;max-width:760px;margin:0 auto}
.hd{display:flex;justify-content:space-between;align-items:baseline;margin:4px 0 12px}
.hd .sym{font-size:15px;color:#8b949e}
.hd .px{font-size:26px;font-weight:700}
.up{color:#f6465d}.dn{color:#2ebd85}
.badge{font-size:13px;padding:2px 8px;border-radius:10px;margin-left:8px}
.bup{background:rgba(246,70,93,.15)}.bdn{background:rgba(46,189,133,.15)}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px}
.card{background:#161b22;border:1px solid #21262d;border-radius:10px;padding:10px 6px;text-align:center}
.card .k{font-size:11px;color:#8b949e;margin-bottom:4px}
.card .v{font-size:15px;font-weight:600}
.panel{background:#161b22;border:1px solid #21262d;border-radius:10px;padding:10px;margin-bottom:12px}
.panel h3{font-size:13px;color:#8b949e;font-weight:500;margin-bottom:6px;display:flex;justify-content:space-between}
.panel h3 span{color:#58a6ff;font-size:11px}
#kchart{height:300px}#fchart{height:160px}
table{width:100%;border-collapse:collapse;font-size:12px}
td{padding:5px 2px;border-bottom:1px solid #21262d;color:#c9d1d9}
.tag{display:inline-block;padding:1px 6px;border-radius:6px;font-size:11px}
.tbuy{background:rgba(246,70,93,.18);color:#f6465d}.tsell{background:rgba(46,189,133,.18);color:#2ebd85}
.thold{background:rgba(139,148,158,.18);color:#8b949e}
.reason{color:#8b949e;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:55%}
.ft{text-align:center;color:#484f58;font-size:11px;padding:8px 0 16px}
.pos{font-size:13px;padding:4px 0;color:#c9d1d9;display:flex;justify-content:space-between}
.stbtn{flex:1;background:#21262d;border:1px solid #30363d;color:#c9d1d9;border-radius:8px;padding:9px 4px;font-size:13px;cursor:pointer}
.stbtn.on{background:rgba(88,166,255,.15);border-color:#58a6ff;color:#58a6ff;font-weight:600}
</style></head><body>
<div class="hd"><div><span class="sym" id="sym">--</span><span class="px" id="px">--</span><span class="badge" id="chg">--</span></div><div style="font-size:11px;color:#8b949e" id="stm">加载中…</div></div>
<div class="cards">
<div class="card"><div class="k">总净值</div><div class="v" id="eq">--</div></div>
<div class="card"><div class="k">累计盈亏</div><div class="v" id="pnl">--</div></div>
<div class="card"><div class="k">持仓市值</div><div class="v" id="mv">--</div></div>
<div class="card"><div class="k">可用现金</div><div class="v" id="cash">--</div></div>
</div>
<div class="panel"><h3>交易策略（点击切换，不清仓）</h3>
<div style="display:flex;gap:8px">
<button class="stbtn" id="btn-dca" onclick="switchStrategy('dca')">🌱 稳健长期定投</button>
<button class="stbtn" id="btn-spec" onclick="switchStrategy('spec')">⚡ 短期投机</button>
<button class="stbtn" id="btn-hybrid" onclick="switchStrategy('hybrid')">🔀 混合并行</button>
</div>
<div id="stdesc" style="font-size:11px;color:#8b949e;margin-top:6px"></div>
</div>
<div class="panel"><h3>K线（日线）<span id="kage"></span></h3><div id="kchart"></div></div>
<div class="panel"><h3>资金动向（每日净买入：红=买方更强，绿=卖方更强）</h3><div id="fchart"></div></div>
<div class="panel"><h3>持仓明细</h3><div id="pos">空仓</div></div>
<div class="panel"><h3>决策流水（最近10条）</h3><table id="dtb"></table></div>
<div class="panel"><h3>成交流水（最近6笔）</h3><table id="ftb"></table></div>
<div class="ft">交易监控台 · 数据来自审计库 · 每10秒刷新 · 仅供研究不构成投资建议</div>
<script>
const TOKEN=location.pathname.split("/")[2];
const API=`/api/${TOKEN}/overview`;
let kc,fc;
function fmt(n,d=2){return Number(n).toLocaleString("zh-CN",{minimumFractionDigits:d,maximumFractionDigits:d})}
function hm(ts){const d=new Date(ts);return isNaN(d)?"":`${String(d.getHours()).padStart(2,"0")}:${String(d.getMinutes()).padStart(2,"0")}`}
function md(ts){const d=new Date(ts);return isNaN(d)?"":`${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`}
const STDESC={dca:"每周定投 + 自动识别回调逢低买入 + 分批/跟踪止盈；SPY 底仓由定投引擎管理",spec:"快讯情绪 + 机构资金动向判断七巨头；重大利空提前抛售 + 按跌幅分级低吸；SPY 底仓冻结",hybrid:"SPY 底仓照常定投 + 新闻引擎并行跑七巨头；利好事件驱动买入、重大利空提前抛售（推荐）"};
function renderStrategy(d){if(!d.strategy)return;
document.getElementById("btn-dca").className="stbtn"+(d.strategy==="dca"?" on":"");
document.getElementById("btn-spec").className="stbtn"+(d.strategy==="spec"?" on":"");
document.getElementById("btn-hybrid").className="stbtn"+(d.strategy==="hybrid"?" on":"");
document.getElementById("stdesc").textContent=STDESC[d.strategy]||""}
async function switchStrategy(name){try{const r=await fetch(`/api/${TOKEN}/strategy`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name})});const d=await r.json();if(!d.ok){alert("切换失败: "+(d.error||"未知错误"));return}}catch(e){alert("切换失败，请重试");return}refresh()}
function cls(v){return v>=0?"up":"dn"}
function tag(a){const m={BUY:["买","tbuy"],SELL:["卖","tsell"],HOLD:["持","thold"]};const t=m[a]||["?","thold"];return `<span class="tag ${t[1]}">${t[0]}</span>`}
function initCharts(){kc=echarts.init(document.getElementById("kchart"));fc=echarts.init(document.getElementById("fchart"));
window.addEventListener("resize",()=>{kc.resize();fc.resize()})}
function renderK(d){const ks=d.klines.slice(-120);
const cats=ks.map(k=>md(k.ts));
kc.setOption({animation:false,backgroundColor:"transparent",
tooltip:{trigger:"axis",axisPointer:{type:"cross"},backgroundColor:"#161b22",borderColor:"#21262d",textStyle:{color:"#e6edf3",fontSize:11}},
axisPointer:{link:[{xAxisIndex:"all"}]},
grid:[{left:52,right:14,top:8,height:185},{left:52,right:14,top:225,height:45}],
xAxis:[{type:"category",data:cats,axisLabel:{color:"#8b949e",fontSize:10},axisLine:{lineStyle:{color:"#21262d"}}},
{type:"category",gridIndex:1,axisLabel:{show:false},axisLine:{show:false}}],
yAxis:[{scale:true,axisLabel:{color:"#8b949e",fontSize:10},splitLine:{lineStyle:{color:"#161b22"}}},
{gridIndex:1,axisLabel:{show:false},splitLine:{show:false}}],
series:[{type:"candlestick",data:ks.map(k=>[k.o,k.c,k.l,k.h]),itemStyle:{color:"#f6465d",color0:"#2ebd85",borderColor:"#f6465d",borderColor0:"#2ebd85"}},
{type:"bar",xAxisIndex:1,yAxisIndex:1,data:ks.map(k=>({value:k.v,itemStyle:{color:k.c>=k.o?"rgba(246,70,93,.5)":"rgba(46,189,133,.5)"}}))}]});
const fl=d.flows;
fc.setOption({animation:false,backgroundColor:"transparent",
tooltip:{trigger:"axis",backgroundColor:"#161b22",borderColor:"#21262d",textStyle:{color:"#e6edf3",fontSize:11}},
grid:{left:52,right:14,top:6,height:110},
xAxis:{type:"category",data:fl.map(f=>hm(f.ts)),axisLabel:{color:"#8b949e",fontSize:9},axisLine:{lineStyle:{color:"#21262d"}}},
yAxis:{axisLabel:{color:"#8b949e",fontSize:9},splitLine:{lineStyle:{color:"#161b22"}}},
series:[{type:"bar",data:fl.map(f=>({value:f.net,itemStyle:{color:f.net>=0?"#f6465d":"#2ebd85"}}))}]})}
async function refresh(){try{const r=await fetch(API);const d=await r.json();if(d.error){document.getElementById("stm").textContent="暂无数据";return}
renderStrategy(d);
document.getElementById("sym").textContent=d.symbol+" ·";
const px=document.getElementById("px");px.textContent=fmt(d.price);px.className="px "+cls(d.chg24h);
const chg=document.getElementById("chg");chg.textContent=(d.chg24h>=0?"+":"")+d.chg24h.toFixed(2)+"%";chg.className="badge "+(d.chg24h>=0?"bup":"bdn");
document.getElementById("stm").textContent="更新 "+d.server_time;
const a=d.account;const eq=document.getElementById("eq");eq.textContent=fmt(a.equity);eq.className="v "+cls(a.pnl);
const pnl=document.getElementById("pnl");pnl.textContent=(a.pnl>=0?"+":"")+fmt(a.pnl)+" ("+(a.pnl_pct>=0?"+":"")+a.pnl_pct.toFixed(2)+"%)";pnl.className="v "+cls(a.pnl);
const mv=a.equity-a.cash;const mvEl=document.getElementById("mv");mvEl.textContent=fmt(mv);mvEl.className="v";
document.getElementById("cash").textContent=fmt(a.cash);
document.getElementById("kage").textContent=d.kline_age_min!=null?`K线新鲜度 ${d.kline_age_min}分`:"";
const pos=document.getElementById("pos");
pos.innerHTML=d.positions.length?d.positions.map(p=>`<div class="pos"><span>${p.symbol}</span><span>${p.qty.toFixed(6)} @ ${fmt(p.avg_price)}</span><span class="${cls(p.pnl_pct)}">${p.pnl_pct>=0?"+":""}${p.pnl_pct}%</span></div>`).join(""):"空仓";
document.getElementById("dtb").innerHTML=d.decisions.map(x=>`<tr><td style="width:40px">${hm(x.ts)}</td><td style="width:26px">${tag(x.action)}</td><td class="reason">${x.reason||""}</td></tr>`).join("")||"<tr><td>暂无</td></tr>";
document.getElementById("ftb").innerHTML=d.fills.map(x=>`<tr><td style="width:40px">${hm(x.ts)}</td><td style="width:26px">${tag(x.side)}</td><td>${x.qty.toFixed(6)} @ ${fmt(x.price)}</td></tr>`).join("")||"<tr><td>暂无</td></tr>";
renderK(d)}catch(e){document.getElementById("stm").textContent="刷新失败，重试中…"}}
initCharts();refresh();setInterval(refresh,10000);
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    token = ""

    def do_GET(self):  # noqa: N802
        from urllib.parse import urlparse

        path = urlparse(self.path).path
        if path == "/health":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # H5 控制台页面（token 防伪）
        if path.startswith(f"/app/{self.token}"):
            body = _HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # 实时数据 JSON API
        if path.startswith(f"/api/{self.token}/overview"):
            payload = json.dumps(_overview_payload(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        # 自托管静态资源（echarts 等）
        if path.startswith("/static/"):
            name = path[len("/static/"):]
            static_dir = ROOT / "dashboard" / "static"
            target = (static_dir / name).resolve()
            if (str(target).startswith(str(static_dir.resolve()))
                    and target.is_file()):
                body = target.read_bytes()
                ctype = ("application/javascript" if name.endswith(".js")
                         else "application/octet-stream")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()
            return
        # /poke/<token>[?cmd=持仓|状态] —— 手机收藏链接，点一下即推送快照到飞书群
        # （飞书自定义机器人收不了群消息，此路由提供"即点即答"替代交互）
        prefix = f"/poke/{self.token}"
        if self.path.startswith(prefix):
            from urllib.parse import parse_qs, urlparse

            qs = parse_qs(urlparse(self.path).query)
            cmd = (qs.get("cmd", [""])[0]) or "状态"
            reply = _dispatch(cmd)
            notifier = load_notifier()
            ok = notifier.send_markdown(reply)
            print(f"[bot] poke 触发({cmd}) -> {'ok' if ok else '失败'}")
            body = (f"<html><head><meta charset='utf-8'><meta name='viewport' "
                    f"content='width=device-width,initial-scale=1'></head><body "
                    f"style='font-family:sans-serif;text-align:center;padding:40px'>"
                    f"<h2>{'✅ 已推送到飞书群' if ok else '❌ 推送失败'}</h2>"
                    f"<p>{reply.splitlines()[0][:60]}</p></body></html>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):  # noqa: N802
        # H5 策略切换：POST /api/<token>/strategy  body {"name": "dca"|"spec"}
        if self.path.startswith(f"/api/{self.token}/strategy"):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length) or b"{}")
                name = str(data.get("name") or "")
                set_strategy(name, source="h5")
                label = strategy_label(name)
                try:
                    load_notifier().send_markdown(
                        f"🧭 **策略切换（H5 监控台）**\n已切换到 **{label}**（{name}），"
                        f"切换不清仓，SPY 底仓保留")
                except Exception:  # noqa: BLE001 —— 通知失败不影响切换结果
                    pass
                body = json.dumps({"ok": True, "strategy": name, "label": label},
                                  ensure_ascii=False).encode("utf-8")
                self.send_response(200)
            except Exception as e:  # noqa: BLE001
                body = json.dumps({"ok": False, "error": str(e)},
                                  ensure_ascii=False).encode("utf-8")
                self.send_response(400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self.path.startswith(f"/feishu/callback/{self.token}"):
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:  # noqa: BLE001
            payload = {}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        # URL 校验握手（飞书配置回调地址时可能下发 challenge）
        if isinstance(payload, dict) and "challenge" in payload:
            try:
                self.wfile.write(json.dumps(
                    {"challenge": payload["challenge"]}).encode())
            except Exception:  # noqa: BLE001
                pass
            return
        text = _find_text(payload)
        print(f"[bot] 收到消息: {text[:80]!r}")
        reply = _dispatch(text)
        notifier = load_notifier()
        ok = notifier.send_markdown(reply)
        print(f"[bot] 已回复({'ok' if ok else '失败'}): {reply.splitlines()[0][:40]}")


def main() -> None:
    token = _load_callback_token()
    _Handler.token = token
    server = ThreadingHTTPServer(("127.0.0.1", PORT), _Handler)
    print(f"[bot] 飞书回调服务已启动 127.0.0.1:{PORT}")
    print(f"[bot] 回调地址（待 nginx 反代后对外）: /feishu/callback/{token[:8]}***")
    server.serve_forever()


if __name__ == "__main__":
    main()

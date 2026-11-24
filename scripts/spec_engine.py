"""短期投机引擎 —— 双策略之一（2026-11-17 起）。

思路：信息流（财经快讯）+ 机构资金动向代理（盘中放量 × 价格方向）判断七巨头。
  重大利空（如"黄仁勋离职"）→ 提前清仓该标的持仓，并进入 72h 重点观察期
  重点观察 / 情绪偏多 + 距 60 日高点跌幅分级 → 按现金比例逢低买入
  综合分 = 0.6×新闻情绪 + 0.4×资金动向；≤ -0.6 清仓，≥ +0.35 具备买入意愿
  SPY 底仓冻结：本引擎永不交易 SPY（切换策略不清仓，底仓归定投引擎管）

数据源（均已实测可用）：
  快讯：ak.stock_info_global_em / _cls / _sina（时间均为北京时间）
  日线：ak.stock_us_daily（qfq，复用 trend_engine.fetch_us_daily）
  分钟：ak.stock_us_hist_min_em（105.前缀；时间列=北京时间，收盘集合竞价
        bar 巨量 → 当日量按"时段进度"归一折算，避免开盘早期误判放量）

工程要点：
  - 每 20 分钟一轮（systemd trading-spec.timer）；只采纳最近 40 分钟快讯
  - 突发快车道：每 5 分钟轻量抓快讯（--news-only，不请求行情），命中紧急信号立即完整评估
  - LLM 复核候选：重大利空 / 突发紧急 / 强情绪（每轮 ≤ 3 次，按紧急度优先），失败降级规则
  - 紧急度 = 突发标记 + 新鲜度 + 严重性 + 情绪强度；超窗的重要快讯保留到 120 分钟
  - assess()/decide()/flow_score()/rule_score()/news_urgency() 等均为纯函数，单测全覆盖
"""
from __future__ import annotations

import copy
import hashlib
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from trend_engine import SLIPPAGE, fetch_us_daily  # noqa: E402

# ---------------------------------------------------------------- 关键词规则

KEYWORDS = {
    "NVDA": ["英伟达", "辉达", "NVIDIA", "黄仁勋", "Jensen Huang"],
    "TSLA": ["特斯拉", "Tesla", "马斯克", "Musk"],
    "AAPL": ["苹果", "Apple", "iPhone", "库克"],
    "MSFT": ["微软", "Microsoft", "纳德拉"],
    "GOOGL": ["谷歌", "Google", "Alphabet", "Gemini"],
    "AMZN": ["亚马逊", "Amazon", "AWS", "贝索斯"],
    "META": ["Meta", "Facebook", "扎克伯格", "Instagram"],
    "AMD": ["AMD", "超微半导体", "苏姿丰"],
    "AVGO": ["博通", "Broadcom"],
    "TSM": ["台积电", "TSMC"],
    "NFLX": ["奈飞", "Netflix"],
    "ORCL": ["甲骨文", "Oracle"],
    "CRM": ["赛富时", "Salesforce"],
}

# 重大利空词：命中即强制情绪 ≤ -0.8，并触发"提前抛售 + 重点观察"
MAJOR_NEG = ["离职", "辞职", "卸任", "resign", "steps down", "step down",
             "破产", "造假", "欺诈", "退市", "违约", "暴雷", "禁令", "制裁",
             "重大诉讼", "黑客", "数据泄露"]

POS_WORDS = ["利好", "大涨", "暴涨", "新高", "超预期", "获批", "中标", "回购",
             "增持", "突破", "创新高", "surge", "beat", "record", "upgrade",
             "回归", "重返", "IPO", "上市", "新模型", "新品"]
NEG_WORDS = ["利空", "大跌", "暴跌", "新低", "不及预期", "下调", "减持", "抛售",
             "调查", "诉讼", "罚款", "裁员", "离职", "辞职", "miss", "downgrade",
             "plunge", "lawsuit"]

# 突发标记（"快讯"在新浪/东财几乎每条都带，只用于紧急度加权，不用于超窗保留）
BREAKING_MARKERS = ["突发", "快讯", "刚刚", "紧急", "breaking", "flash", "alert", "just in"]
URGENT_KEEP_MARKERS = ["突发", "紧急", "breaking", "flash", "alert"]
SEVERE_NEG = ["暴跌", "闪崩", "崩盘", "跳水", "plunge", "crash", "collapse", "freefall"]
# 辟谣/否认：命中则认为"事件未发生"，不按重大利空处理（防突发误抛售）
DENIAL_WORDS = ["辟谣", "否认", "不实", "谣言", "假消息", "未离职", "未辞职",
                "denies", "denied", "debunk", "false report"]


def match_symbols(text: str) -> list[str]:
    """文本关联到哪些投机标的（英文忽略大小写）。"""
    t = str(text).lower()
    return [sym for sym, words in KEYWORDS.items()
            if any(w.lower() in t for w in words)]


def rule_score(text: str) -> float:
    """关键词情绪分：(利好词数 - 利空词数) / 2，截断到 [-1, 1]。"""
    t = str(text).lower()
    pos = sum(t.count(w.lower()) for w in POS_WORDS)
    neg = sum(t.count(w.lower()) for w in NEG_WORDS)
    return max(-1.0, min(1.0, (pos - neg) / 2))


def has_major_neg(text: str) -> bool:
    t = str(text).lower()
    return any(w.lower() in t for w in MAJOR_NEG)


def is_denial(text: str) -> bool:
    """辟谣/否认类快讯：事件未发生，不应按重大利空抛售。"""
    t = str(text).lower()
    return any(w.lower() in t for w in DENIAL_WORDS)


def news_urgency(text: str, age_min: float | None = None,
                 major_neg: bool = False, cfg: dict | None = None) -> float:
    """突发紧急度 0~1（纯函数）：突发标记 + 新鲜度 + 严重性 + 情绪强度。

    用于三处：突发快车道判定、行情评估优先级、LLM 复核预算分配。
    """
    cfg = cfg or {}
    t = str(text).lower()
    u = 0.0
    if any(w.lower() in t for w in BREAKING_MARKERS):
        u += 0.30
    if age_min is not None:
        if age_min <= 10:
            u += 0.25
        elif age_min <= 30:
            u += 0.15
        elif age_min <= 60:
            u += 0.05
    if major_neg:
        u += 0.35
    if any(w.lower() in t for w in SEVERE_NEG):
        u += 0.10
    if abs(rule_score(t)) >= float(cfg.get("urgency_strong_score", 0.5)):
        u += 0.10
    return min(1.0, round(u, 4))


def _age_min(time_str: str, now: datetime | None = None) -> float | None:
    """快讯时间 → 距今分钟数（北京时间 naive）；缺失/解析失败返回 None。"""
    if not str(time_str or "").strip():
        return None
    t = _parse_news_time(time_str)
    if t is None:
        return None
    return max(0.0, ((now or _now_beijing()) - t).total_seconds() / 60)


def parse_llm_score(text: str) -> float | None:
    """从 LLM 回复中解析 -1~1 情绪分；解析失败返回 None（降级规则分）。"""
    m = re.search(r"-?\d+(?:\.\d+)?", str(text))
    if not m:
        return None
    try:
        return max(-1.0, min(1.0, float(m.group())))
    except ValueError:
        return None


# ---------------------------------------------------------------- 信息层

def _now_beijing() -> datetime:
    """北京时间 naive（快讯时间列的时区基准）。"""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        return (datetime.now(timezone.utc) + timedelta(hours=8)).replace(tzinfo=None)


def _parse_news_time(s: str) -> datetime | None:
    s = str(s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y/%m/%d %H:%M:%S", "%m-%d %H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.year == 1900:                     # 无年份格式补当前年
                dt = dt.replace(year=_now_beijing().year)
            return dt
        except ValueError:
            continue
    return None


def news_fingerprint(item: dict) -> str:
    text = str(item.get("title") or item.get("body") or "")
    return hashlib.md5((text + str(item.get("source") or "")).encode("utf-8")).hexdigest()[:16]


def fetch_news() -> list[dict]:
    """三源快讯合并（东财/财联社/新浪）。单源失败不阻断其余源。"""
    import akshare as ak

    items: list[dict] = []
    try:
        df = ak.stock_info_global_em()
        for _, r in df.iterrows():
            items.append({"title": str(r.get("标题") or ""),
                          "body": str(r.get("摘要") or ""),
                          "time": str(r.get("发布时间") or ""), "source": "em"})
    except Exception as e:  # noqa: BLE001
        print(f"[投机引擎] 东财快讯源失败: {e}")
    try:
        df = ak.stock_info_global_cls()
        for _, r in df.iterrows():
            items.append({"title": str(r.get("标题") or ""),
                          "body": str(r.get("内容") or ""),
                          "time": f"{r.get('发布日期') or ''} {r.get('发布时间') or ''}".strip(),
                          "source": "cls"})
    except Exception as e:  # noqa: BLE001
        print(f"[投机引擎] 财联社快讯源失败: {e}")
    try:
        df = ak.stock_info_global_sina()
        for _, r in df.iterrows():
            items.append({"title": "", "body": str(r.get("内容") or ""),
                          "time": str(r.get("时间") or ""), "source": "sina"})
    except Exception as e:  # noqa: BLE001
        print(f"[投机引擎] 新浪快讯源失败: {e}")
    return items


def filter_news(items: list[dict], seen: list[str], lookback_min: int,
                now: datetime | None = None,
                urgent_lookback_min: int | None = None) -> list[dict]:
    """去重（指纹）+ 时间过滤（只留最近 N 分钟；时间解析失败的保留）。

    突发保留：超过常规窗口但仍在 urgent_lookback_min 内的重要快讯
    （重大利空词 / 突发紧急标记）不被丢弃，避免上一轮抓取失败漏掉急讯。
    now 为北京时间 naive；返回的每条 item 附加 fp 字段。
    """
    now = now or _now_beijing()
    out: list[dict] = []
    for it in items:
        fp = news_fingerprint(it)
        if fp in seen:
            continue
        t = _parse_news_time(it.get("time", ""))
        if t is not None:
            age = (now - t).total_seconds() / 60
            if age > lookback_min:
                text = f"{it.get('title') or ''} {it.get('body') or ''}".lower()
                keep = (urgent_lookback_min is not None
                        and age <= float(urgent_lookback_min)
                        and (has_major_neg(text)
                             or any(w.lower() in text for w in URGENT_KEEP_MARKERS)))
                if not keep:
                    continue
        out.append({**it, "fp": fp})
    return out


def _to_et(dt_bj_naive: datetime) -> datetime:
    """北京时间 naive → 美东时间（aware）。zoneinfo 失败回退固定 UTC-5。"""
    try:
        from zoneinfo import ZoneInfo
        return dt_bj_naive.replace(tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(
            ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001
        return (dt_bj_naive - timedelta(hours=13)).replace(tzinfo=timezone.utc)


def fetch_intraday(symbol: str) -> dict | None:
    """盘中分钟线 → {price, vol_today, frac, et_date}。

    price   = 最后一根分钟 bar 的收盘价（"最新价"列是滚动均价，弃用）
    vol_today = 最新 bar 所属 ET 交易日的累计成交量
    frac    = 该交易日时段进度（9:30~16:00 归一，收盘后=1），用于折算全日量
    """
    import akshare as ak

    start = (_now_beijing() - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
    df = ak.stock_us_hist_min_em(symbol=f"105.{symbol}", start_date=start,
                                 end_date="2222-01-01 09:32:00")
    if df is None or len(df) == 0:
        return None
    bars = []
    for _, r in df.iterrows():
        t = _parse_news_time(str(r.get("时间") or ""))
        if t is None:
            continue
        bars.append((_to_et(t), float(r.get("收盘") or 0), float(r.get("成交量") or 0)))
    if not bars:
        return None
    et_last, price, _ = bars[-1]
    et_date = et_last.date()
    vol_today = sum(v for et, _, v in bars if et.date() == et_date)
    try:
        from zoneinfo import ZoneInfo
        today_et = datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:  # noqa: BLE001
        today_et = (datetime.now(timezone.utc) - timedelta(hours=5)).date()
    if et_date < today_et:
        frac = 1.0                                  # 最新 bar 是上一交易日（已收盘）
    else:
        minutes = et_last.hour * 60 + et_last.minute
        frac = max(0.05, min(1.0, (minutes - 570) / 390))
    return {"price": price, "vol_today": vol_today, "frac": frac,
            "et_date": et_date.isoformat()}


# ---------------------------------------------------------------- 决策层（纯函数）

def flow_score(vol_today: float, avg20: float, chg: float, frac: float,
               spike: float = 2.0) -> float:
    """机构资金动向代理：折算全日量 / 20日均量 ≥ spike 才算放量。

    放量方向由当日涨跌决定：放量上涨=资金抢筹(+)，放量下跌=机构出货(-)。
    强度 = min(1, (ratio-1)/2)；未放量返回 0。
    """
    if avg20 <= 0 or vol_today <= 0:
        return 0.0
    ratio = (vol_today / max(frac, 0.05)) / avg20
    if ratio < spike:
        return 0.0
    sign = 1.0 if chg > 0 else (-1.0 if chg < 0 else 0.0)
    return sign * min(1.0, (ratio - 1) / 2)


def assess(symbol: str, headlines: list[dict], intra: dict,
           candles: list, cfg: dict, now: datetime | None = None) -> dict:
    """单标的评估（纯函数）。headlines=已关联该标的的快讯（含 fp）。

    now 为北京时间 naive（缺省取当前）；用于计算快讯新鲜度与突发紧急度。
    """
    price = float(intra["price"])
    et_date = str(intra["et_date"])
    now = now or _now_beijing()

    # 新闻情绪：取绝对值最强一条；重大利空强制 ≤ -0.8
    # 突发紧急度：识别最"急"的一条，决定 LLM 优先级与即时预警
    news, headline, fp, major = 0.0, "", "", False
    urgency, age_min, best_key = 0.0, None, (-1, -1.0)
    scored: list[tuple[float, str]] = []
    for it in headlines:
        text = f"{it.get('title') or ''} {it.get('body') or ''}".strip()
        s = rule_score(text)
        m = has_major_neg(text) and not is_denial(text)
        age = _age_min(it.get("time", ""), now)
        u = news_urgency(text, age, m, cfg)
        scored.append((u, text))
        if abs(s) > abs(news):
            news, fp = s, str(it.get("fp") or "")
        if m:
            major = True
        if u > urgency:
            urgency = u
        # 展示/告警用的头条：重大利空优先，其次紧急度（实际事件优先于普通快讯）
        key = (1 if m else 0, u)
        if key > best_key:
            best_key, headline, age_min = key, text, age
    if major:
        news = min(news, -0.8)

    # 资金动向：以"最新盘中 bar 的 ET 日期"为当日，prev_close 取其前一日日线收盘
    prev_close, prev_idx = 0.0, -1
    for i in range(len(candles) - 1, -1, -1):
        if candles[i].ts.strftime("%Y-%m-%d") < et_date:
            prev_close, prev_idx = float(candles[i].close), i
            break
    if prev_idx < 0 and candles:
        prev_close, prev_idx = float(candles[-1].close), len(candles) - 1
    chg = price / prev_close - 1 if prev_close > 0 else 0.0
    vols = [float(c.volume) for c in candles[max(0, prev_idx - 20):prev_idx]]
    avg20 = sum(vols) / len(vols) if vols else 0.0
    flow = flow_score(float(intra["vol_today"]), avg20, chg,
                      float(intra["frac"]), float(cfg.get("volume_spike", 2.0)))

    w_news = float(cfg.get("w_news", 0.6))
    w_flow = float(cfg.get("w_flow", 0.4))
    score = w_news * news + w_flow * flow

    high60 = max((float(c.high) for c in candles[-60:]), default=price)
    drop = 1 - price / high60 if high60 > 0 else 0.0
    tier = None
    for t in sorted(cfg.get("buy_tiers") or [], key=lambda x: float(x.get("drop", 0))):
        if drop >= float(t.get("drop", 0)):
            tier = t
    urgent = urgency >= float(cfg.get("urgent_threshold", 0.60))
    heads = [text for _, text in sorted(scored, key=lambda x: x[0], reverse=True)[:3] if text]
    return {"symbol": symbol, "news_score": news, "flow_score": flow,
            "score": score, "major_neg": major, "headline": headline,
            "headlines": heads, "urgency": urgency, "urgent": urgent,
            "news_age_min": round(age_min, 1) if age_min is not None else None,
            "fp": fp, "drop": drop, "tier": tier, "price": price}


def decide(pf: dict, assessments: list[dict], cfg: dict,
           now_iso: str) -> tuple[dict, list[dict]]:
    """组合级决策（纯函数，深拷贝）。先清过期观察 → 先卖 → 后买。

    卖：持仓（SPY 除外）遇重大利空或综合分 ≤ sell_score → 全卖。
    买：未持仓 + 冷却期外 + 持仓数上限内 + (重大利空/观察期/综合分达标/重大利好事件)
        + 跌幅分级命中（或 LLM 强利好的小仓位事件买入）→ 按现金比例买入。
    """
    st = copy.deepcopy(pf)
    spec = st.setdefault("spec", {})
    pos_all = st.setdefault("positions", {})
    now = datetime.fromisoformat(now_iso)
    sell_score = float(cfg.get("sell_score", -0.6))
    buy_score = float(cfg.get("buy_score", 0.35))
    cooldown_min = float(cfg.get("trade_cooldown_min", 60))
    max_pos = int(cfg.get("max_positions", 3))
    min_usd = float(cfg.get("min_trade_usd", 500))
    watch_hours = float(cfg.get("watch_hours", 72))
    event_buy_score = float(cfg.get("event_buy_score", 0.6))
    event_buy_cash_pct = float(cfg.get("event_buy_cash_pct", 0.08))

    # 清理过期观察期
    watch = {}
    for sym, w in (spec.get("watch") or {}).items():
        try:
            if datetime.fromisoformat(str(w.get("until") or "")) > now:
                watch[sym] = w
        except ValueError:
            continue

    trades: list[dict] = []
    last_trade = dict(spec.get("last_trade") or {})

    def _cool_ok(sym: str) -> bool:
        ts = last_trade.get(sym)
        if not ts:
            return True
        try:
            return (now - datetime.fromisoformat(str(ts))).total_seconds() >= cooldown_min * 60
        except ValueError:
            return True

    # ---- 先卖：重大利空提前抛售 / 综合分清仓 ----
    for a in assessments:
        sym = a["symbol"]
        if sym == "SPY":
            continue                                # SPY 底仓冻结，永不交易
        pos = pos_all.get(sym)
        if not pos or pos.get("qty", 0) <= 0:
            continue
        if a["major_neg"] or a["score"] <= sell_score:
            qty = pos["qty"]
            px = a["price"] * (1 - SLIPPAGE)
            avg = float(pos.get("avg_price") or 0)
            why = (f"重大利空提前抛售: {a['headline'][:60]}" if a["major_neg"]
                   else f"综合分{a['score']:+.2f}≤{sell_score}，清仓离场")
            trades.append({"kind": "sell", "symbol": sym, "qty": qty, "px": px,
                           "fee": 0.0, "pnl": (px / avg - 1) * 100 if avg > 0 else 0.0,
                           "reason": f"投机卖出 {sym}: {why}"})
            st["cash"] += qty * px
            pos_all.pop(sym, None)
            last_trade[sym] = now_iso

    # ---- 后买：跌幅分级调整买入意愿 ----
    for a in assessments:
        sym = a["symbol"]
        if sym == "SPY" or sym in pos_all:
            continue
        if not _cool_ok(sym):
            continue
        n_spec = sum(1 for s in pos_all if s != "SPY")
        if n_spec >= max_pos or st["cash"] < min_usd:
            continue
        willing = a["major_neg"] or sym in watch or a["score"] >= buy_score
        # 重大利好事件买入：仅 LLM 强利好(≥event_buy_score)才允许无回调小仓位买入；
        # 普通利好仍须命中回调跌幅，避免盲目追涨
        event_buy = bool(a.get("llm_used")) and float(a.get("news_score") or 0.0) >= event_buy_score
        tier = a.get("tier")
        if not (willing or event_buy):
            continue
        if tier is not None:
            cash_pct = float(tier.get("cash_pct", 0))
        elif event_buy:
            cash_pct = event_buy_cash_pct
        else:
            continue
        usd = st["cash"] * cash_pct
        if usd < min_usd:
            continue
        usd = min(usd, st["cash"])
        px = a["price"] * (1 + SLIPPAGE)
        qty = round(usd / px, 4)
        if qty <= 0:
            continue
        pos_all[sym] = {"qty": qty, "avg_price": px, "trail_high": px, "tp_stage": 0}
        st["cash"] -= qty * px
        last_trade[sym] = now_iso
        src = ("重大利空观察期低吸" if a["major_neg"]
               else "重点观察期低吸" if sym in watch
               else f"重大利好事件买入(LLM情绪{a['news_score']:+.2f})" if event_buy and tier is None
               else f"回调分级低吸(综合分{a['score']:+.2f})")
        trades.append({"kind": "buy", "symbol": sym, "qty": qty, "px": px,
                       "fee": 0.0,
                       "reason": f"投机买入 {sym}: 距60日高点跌{a['drop'] * 100:.1f}%，"
                                 f"{src}，投入现金{cash_pct:.0%}"})

    # ---- 重大利空 → 重点观察期（无论是否持仓都记录）----
    for a in assessments:
        if a["major_neg"]:
            watch[a["symbol"]] = {
                "until": (now + timedelta(hours=watch_hours)).isoformat(),
                "headline": a["headline"][:120]}

    spec["watch"] = watch
    spec["last_trade"] = last_trade
    return st, trades


# ---------------------------------------------------------------- LLM 增强

def llm_candidates(assessments: list[dict], cfg: dict | None = None) -> list[dict]:
    """LLM 分类候选（纯函数）：凡有快讯关联的标的都送 LLM 判情绪。

    不再只复核"关键词已判重大/紧急/强情绪"的候选——利好/中性事件（高管回归、
    新模型、IPO 等）关键词分≈0，同样需要 LLM 识别，否则永不触发买入。
    优先级：重大利空(安全复核) > 突发紧急 > 快讯越新 > 紧急度；预算内尽量全覆盖。
    """
    out = [a for a in assessments if a.get("headline")]

    def key(a):
        age = a.get("news_age_min")
        age = float(age) if age is not None else 1e9
        return (bool(a.get("major_neg")), bool(a.get("urgent")), -age,
                float(a.get("urgency") or 0.0))

    return sorted(out, key=key, reverse=True)


def enhance_with_llm(assessments: list[dict], cfg: dict,
                     http_post=None) -> int:
    """对候选快讯用 LLM 判情绪分（每轮 ≤ llm_max_calls 次，按优先级）。

    候选含利好/中性事件（不再仅限重大利空），使高管回归、新模型等也能被识别。
    重大利空保留"只能加深不能减弱"的抛售纪律（min(score, -0.8)）；
    其余候选以 LLM 情绪分替换规则分并重算综合分。任何失败静默降级关键词规则分。
    http_post 可注入（单测用），缺省 httpx.post。返回实际调用成功次数。
    """
    from common.llm_config import load_llm_config

    lc = load_llm_config()
    if not lc.available:
        return 0
    if http_post is None:
        import httpx

        http_post = httpx.post

    budget = int(cfg.get("llm_max_calls", 6))
    w_news = float(cfg.get("w_news", 0.6))
    w_flow = float(cfg.get("w_flow", 0.4))
    done = 0
    for a in llm_candidates(assessments, cfg):
        if done >= budget:
            break
        heads = (a.get("headlines") or [a["headline"]])[:3]
        news_block = "\n".join(f"{i}. {h}" for i, h in enumerate(heads, 1))
        try:
            body = {"model": lc.model,
                    "messages": [
                        {"role": "system",
                         "content": "你是美股短线突发新闻情绪分析器。综合给出的多条快讯，"
                                    "判断对股票未来 1-3 个交易日的影响。只输出一个 -1 到 1 "
                                    "之间的数字：-1=极度利空（暴跌/核心高管离职/监管重罚），"
                                    "1=极度利好（超预期业绩/大单/获批），0=中性或无法判断。"
                                    "若新闻是辟谣、传闻未证实或与公司无关，按实际影响给分，"
                                    "不要因为单个词过度反应。不要输出任何其他文字。"},
                        {"role": "user",
                         "content": f"股票: {a['symbol']}\n"
                                    f"突发快讯（按紧急度排序）:\n{news_block}"}],
                    "temperature": 0}
            if lc.extra_body:
                body.update(lc.extra_body)
            r = http_post(f"{lc.base_url.rstrip('/')}/chat/completions",
                          headers={"Authorization": f"Bearer {lc.api_key}"},
                          json=body, timeout=lc.timeout)
            text = r.json()["choices"][0]["message"]["content"]
            s = parse_llm_score(text)
            if s is None:
                continue
            a["news_score"] = min(s, -0.8) if a["major_neg"] else s
            a["score"] = w_news * a["news_score"] + w_flow * float(a.get("flow_score") or 0.0)
            a["llm_used"] = True
            done += 1
        except Exception as e:  # noqa: BLE001
            print(f"[投机引擎] LLM 增强失败({a['symbol']}): {e}")
    return done


# ---------------------------------------------------------------- 编排层

def urgent_headlines(items: list[dict], cfg: dict | None = None,
                     now: datetime | None = None) -> list[dict]:
    """突发扫描（纯函数）：紧急度达标的快讯 → 关联标的清单（按紧急度降序）。"""
    cfg = cfg or {}
    now = now or _now_beijing()
    thr = float(cfg.get("urgent_threshold", 0.60))
    out: list[dict] = []
    for it in items:
        text = f"{it.get('title') or ''} {it.get('body') or ''}".strip()
        major = has_major_neg(text) and not is_denial(text)
        u = news_urgency(text, _age_min(it.get("time", ""), now), major, cfg)
        if u < thr:
            continue
        for sym in match_symbols(text):
            out.append({"symbol": sym, "urgency": u, "major_neg": major,
                        "headline": text[:120], "fp": str(it.get("fp") or "")})
    return sorted(out, key=lambda x: x["urgency"], reverse=True)


def need_full_run(urgent: list[dict], held: set[str] | None = None) -> bool:
    """突发快车道是否立即跑完整评估（纯函数）。

    触发条件：有持仓标的被突发命中（要抢在下跌前处置），
    或重大利空命中任意标的（要建观察期/低吸意愿）。
    """
    held = set(held or ())
    return any(it.get("major_neg") or it.get("symbol") in held for it in urgent)


def sort_symbols(symbols: list[str], by_sym: dict[str, list[dict]],
                 cfg: dict | None = None, now: datetime | None = None) -> list[str]:
    """按新闻紧急度排序标的（纯函数，稳定排序）：紧急标的先抓行情、先评估。

    不改变评估集合，只缩短突发标的从"快讯命中"到"决策落盘"的时延。
    """
    cfg = cfg or {}
    now = now or _now_beijing()
    thr = float(cfg.get("urgent_threshold", 0.60))

    def key(sym: str) -> tuple[int, int, float]:
        urg, major = 0.0, 0
        for it in (by_sym.get(sym) or []):
            text = f"{it.get('title') or ''} {it.get('body') or ''}".strip()
            m = has_major_neg(text) and not is_denial(text)
            urg = max(urg, news_urgency(text, _age_min(it.get("time", ""), now), m, cfg))
            major = max(major, 1 if m else 0)
        return (1 if urg >= thr else 0, major, urg)

    return sorted(symbols, key=key, reverse=True)


def load_cfg() -> dict:
    try:
        import yaml

        data = yaml.safe_load((ROOT / "config" / "risk_limits.yaml")
                              .read_text(encoding="utf-8")) or {}
        return data.get("spec_engine") or {}
    except Exception:  # noqa: BLE001
        return {}


def main(argv: list[str] | None = None) -> int:
    from common.strategy_switch import (acquire_portfolio_lock, load_portfolio,
                                        load_strategy, save_portfolio)

    if load_strategy() not in ("spec", "hybrid"):
        print("[投机引擎] 当前策略未启用事件投机，跳过")
        return 0
    args = list(sys.argv[1:] if argv is None else argv)
    news_only = "--news-only" in args
    # 与 dca 共享组合锁：完整轮阻塞等待，快车道非阻塞（被占用则本轮跳过）
    lock = acquire_portfolio_lock(blocking=not news_only)
    if lock is None:
        print("[投机引擎] 已有实例在运行，本轮跳过")
        return 0
    cfg = load_cfg()
    symbols = list(cfg.get("symbols") or list(KEYWORDS))
    pf = load_portfolio()

    fresh = filter_news(fetch_news(), pf["spec"].get("seen") or [],
                        int(cfg.get("news_lookback_min", 40)),
                        urgent_lookback_min=int(cfg.get("urgent_lookback_min", 120)))

    # 突发快车道（--news-only，5 分钟一轮）：只抓快讯，命中紧急信号才跑完整评估
    if news_only:
        urgent = urgent_headlines(fresh, cfg)
        held = {s for s in (pf.get("positions") or {}) if s != "SPY"}
        if not need_full_run(urgent, held):
            print(f"[投机引擎] 突发扫描: 新快讯 {len(fresh)} 条，无紧急信号")
            return 0
        print(f"[投机引擎] 突发扫描: 紧急信号 {len(urgent)} 个 → 立即完整评估")

    by_sym: dict[str, list[dict]] = {s: [] for s in symbols}
    for it in fresh:
        text = f"{it.get('title') or ''} {it.get('body') or ''}"
        for s in match_symbols(text):
            if s in by_sym:
                by_sym[s].append(it)

    assessments, candles_by_sym = [], {}
    for sym in sort_symbols(symbols, by_sym, cfg):
        try:
            candles = fetch_us_daily(sym, limit=120)
            intra = fetch_intraday(sym)
        except Exception as e:  # noqa: BLE001
            print(f"[投机引擎] {sym} 行情获取失败，跳过: {e}")
            continue
        if not candles or intra is None or intra["price"] <= 0:
            print(f"[投机引擎] {sym} 行情不足，跳过")
            continue
        candles_by_sym[sym] = candles
        assessments.append(assess(sym, by_sym[sym], intra, candles, cfg))
    if not assessments:
        print("[投机引擎] 本轮无可用行情，跳过")
        return 0
    enhance_with_llm(assessments, cfg)

    now_iso = datetime.now(timezone.utc).isoformat()
    pf, trades = decide(pf, assessments, cfg, now_iso)

    from common.events import Action, DecisionEvent, FillEvent, KlineEvent, Market, OrderStatus
    from common.notifier import load_notifier
    from decision_layer.audit import AuditLog

    audit = AuditLog(str(ROOT / "data" / "audit.db"))
    for sym, candles in candles_by_sym.items():
        audit.backfill_klines(KlineEvent(
            symbol=sym, market=Market.US_STOCK, interval="1d",
            current=None, history=candles[-120:]))
    now = datetime.now(timezone.utc)
    for t in trades:
        act = Action.BUY if t["kind"] == "buy" else Action.SELL
        dec = DecisionEvent(
            ts=now, action=act, symbol=t["symbol"], market=Market.US_STOCK,
            quantity=t["qty"], reference_price=t["px"],
            confidence=min(1.0, abs(next((a["score"] for a in assessments
                                          if a["symbol"] == t["symbol"]), 0.5))),
            reason=t["reason"])
        audit.log_decision(dec)
        # order_id 是 fills 表主键：必须唯一，否则 INSERT OR REPLACE 覆盖上一笔成交
        audit.log_fill(FillEvent(
            ts=now, symbol=t["symbol"], market=Market.US_STOCK, side=act,
            filled_qty=t["qty"], avg_price=t["px"],
            commission=t["fee"], status=OrderStatus.FILLED,
            decision_id=dec.id, order_id=dec.id))

    # 状态收尾：快讯指纹滚动去重窗口 + 本轮运行时间
    pf["spec"]["seen"] = ((pf["spec"].get("seen") or [])
                          + [it["fp"] for it in fresh])[-300:]
    pf["spec"]["last_run"] = now_iso

    price_map = {a["symbol"]: a["price"] for a in assessments}
    eq = pf["cash"]
    for sym, p in pf["positions"].items():
        if sym == "SPY":
            px = float(pf["dca"].get("last_price") or 0) or float(p.get("avg_price") or 0)
        else:
            px = price_map.get(sym) or float(p.get("avg_price") or 0)
        eq += p.get("qty", 0) * px
    pf["high_water"] = max(float(pf.get("high_water") or 0), eq)
    save_portfolio(pf)
    audit.close()

    print(f"[投机引擎] 评估 {len(assessments)} 标的 | 新快讯 {len(fresh)} 条 | "
          f"净值 {eq:,.0f} USD" + ("（本次有交易）" if trades else ""))
    notifier = load_notifier()
    if trades:
        lines = ["⚡ **短期投机引擎交易**"]
        lines += [t["reason"] for t in trades]
        lines.append(f"净值 {eq:,.0f} USD")
        notifier.send_markdown("\n".join(lines))
    for a in assessments:
        if a["major_neg"]:
            notifier.send_markdown(
                f"🚨 **重大利空预警: {a['symbol']}**\n{a['headline'][:120]}\n"
                f"情绪分 {a['news_score']:+.2f}，紧急度 {a.get('urgency', 0):.2f}，"
                f"已进入 {cfg.get('watch_hours', 72)}h 重点观察期"
                f"（持仓提前抛售，后续按跌幅分级低吸）",
                dedup_key=f"spec_neg_{a['fp'] or a['symbol']}")
        elif a.get("urgent"):
            notifier.send_markdown(
                f"⚡ **突发快讯: {a['symbol']}**\n{a['headline'][:120]}\n"
                f"紧急度 {a.get('urgency', 0):.2f}，本轮已优先评估"
                f"（买卖仍按策略纪律执行）",
                dedup_key=f"spec_urgent_{a['fp'] or a['symbol']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

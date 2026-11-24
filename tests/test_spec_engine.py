"""短期投机引擎单测 —— 关键词规则 / 资金代理 / assess / decide / LLM 增强全覆盖。

CFG 数值与 config/risk_limits.yaml 的 spec_engine 段一致。
核心场景：黄仁勋离职 → 提前抛售 NVDA + 72h 重点观察 + 按跌幅分级低吸；SPY 永不被交易。
2026-11-17 增补：突发紧急度 / 突发快车道 / LLM 候选优先级与预算。
"""
import copy
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from common.events import Candle
from common.strategy_switch import default_portfolio
from scripts.spec_engine import (assess, decide, enhance_with_llm, filter_news,
                                 flow_score, has_major_neg, is_denial,
                                 llm_candidates, match_symbols, need_full_run,
                                 news_fingerprint, news_urgency, parse_llm_score,
                                 rule_score, sort_symbols, urgent_headlines)
from scripts.trend_engine import SLIPPAGE

CFG = {"w_news": 0.6, "w_flow": 0.4, "volume_spike": 2.0,
       "sell_score": -0.6, "buy_score": 0.35,
       "buy_tiers": [{"drop": 0.08, "cash_pct": 0.10},
                     {"drop": 0.15, "cash_pct": 0.15},
                     {"drop": 0.25, "cash_pct": 0.20}],
       "watch_hours": 72, "max_positions": 3, "trade_cooldown_min": 60,
       "min_trade_usd": 500,
       "urgent_threshold": 0.60, "urgent_lookback_min": 120,
       "urgency_strong_score": 0.5, "llm_strong_score": 0.5, "llm_max_calls": 3}

NOW = "2026-11-17T02:00:00+00:00"
BJ_NOW = datetime(2026, 11, 17, 10, 0, 0)          # 北京时间 naive（快讯时间基准）
TIER1 = {"drop": 0.08, "cash_pct": 0.10}


def _candles(n=30, close=100.0, high=110.0, vol=1e6):
    base = datetime(2026, 10, 1, tzinfo=timezone.utc)
    return [Candle(ts=base + timedelta(days=i), open=close, high=high,
                   low=close * 0.95, close=close, volume=vol, closed=True)
            for i in range(n)]


def _intra(price=100.0, vol=1e6, frac=1.0, et_date="2026-11-16"):
    return {"price": price, "vol_today": vol, "frac": frac, "et_date": et_date}


def _a(sym, score=0.0, major=False, price=100.0, drop=0.10,
       tier=None, headline="测试快讯", urgent=False, urgency=0.0):
    return {"symbol": sym, "news_score": score, "flow_score": 0.0,
            "score": score, "major_neg": major, "headline": headline,
            "headlines": [headline] if headline else [],
            "urgency": urgency, "urgent": urgent,
            "fp": "fp_" + sym, "drop": drop,
            "tier": TIER1 if tier is None else tier, "price": price}


def _pos(pf, sym, qty=10.0, avg=150.0):
    pf["positions"][sym] = {"qty": qty, "avg_price": avg,
                            "trail_high": avg, "tp_stage": 0}
    return pf


class TestKeywords:
    def test_match_cn(self):
        assert match_symbols("英伟达大涨，特斯拉回调") == ["NVDA", "TSLA"]

    def test_match_en_case_insensitive(self):
        assert match_symbols("nvidia and APPLE drop") == ["NVDA", "AAPL"]

    def test_match_none(self):
        assert match_symbols("今天天气不错") == []

    def test_rule_score_math_and_clamp(self):
        assert rule_score("利好 大跌") == 0.0
        assert rule_score("超预期 获批") == 1.0
        assert rule_score("利好 利好 利好 利好") == 1.0    # 截断到 1
        assert rule_score("暴跌 暴跌 暴跌") == -1.0       # 截断到 -1

    def test_major_neg_words(self):
        assert has_major_neg("黄仁勋离职") is True
        assert has_major_neg("CEO resigns") is True
        assert has_major_neg("英伟达创新高") is False

    def test_parse_llm_score(self):
        assert parse_llm_score("-0.85") == -0.85
        assert parse_llm_score("情绪分: 0.7") == 0.7
        assert parse_llm_score("没有数字") is None
        assert parse_llm_score("2.5") == 1.0
        assert parse_llm_score("-3") == -1.0


class TestFlowScore:
    def test_no_spike_zero(self):
        assert flow_score(1.5e6, 1e6, 0.02, 1.0) == 0.0   # ratio 1.5 < 2

    def test_spike_up_full(self):
        assert flow_score(3e6, 1e6, 0.02, 1.0) == 1.0     # (3-1)/2 = 1

    def test_spike_down(self):
        assert flow_score(3e6, 1e6, -0.02, 1.0) == -1.0   # 放量下跌=机构出货

    def test_frac_normalization(self):
        assert flow_score(1e6, 1e6, 0.02, 0.5) == 0.5     # 半日量折算全日=2倍

    def test_partial_strength(self):
        assert flow_score(2.5e6, 1e6, 0.01, 1.0) == 0.75

    def test_flat_chg_zero(self):
        assert flow_score(3e6, 1e6, 0.0, 1.0) == 0.0

    def test_bad_inputs_zero(self):
        assert flow_score(0, 1e6, 0.02, 1.0) == 0.0
        assert flow_score(3e6, 0, 0.02, 1.0) == 0.0


class TestAssess:
    def test_major_neg_forced_score(self):
        heads = [{"title": "英伟达CEO黄仁勋离职", "body": "", "fp": "x"}]
        a = assess("NVDA", heads, _intra(), _candles(), CFG)
        assert a["major_neg"] is True
        assert a["news_score"] == -0.8, "重大利空强制 ≤ -0.8"
        assert abs(a["score"] - (-0.48)) < 1e-9           # 0.6×(-0.8)+0.4×0
        assert a["tier"]["cash_pct"] == 0.10              # 跌9.1%命中8%档

    def test_composite_news_and_flow(self):
        heads = [{"title": "英伟达超预期", "body": "", "fp": "x"}]
        a = assess("NVDA", heads, _intra(price=102.0, vol=3e6), _candles(), CFG)
        assert a["news_score"] == 0.5
        assert a["flow_score"] == 1.0                     # 放量上涨
        assert abs(a["score"] - 0.7) < 1e-9               # 0.6×0.5+0.4×1
        assert a["tier"] is None                          # 跌7.3%未达8%档

    def test_no_news_no_flow_zero(self):
        a = assess("NVDA", [], _intra(), _candles(), CFG)
        assert a["score"] == 0.0 and a["major_neg"] is False


class TestFilterNews:
    def test_dedup_lookback_and_bad_time(self):
        now = datetime(2026, 11, 17, 10, 0, 0)            # 北京时间 naive
        fresh = {"title": "A", "time": "2026-11-17 09:50:00", "source": "em"}
        old = {"title": "B", "time": "2026-11-17 08:00:00", "source": "em"}
        bad = {"title": "C", "time": "刚刚", "source": "cls"}
        seen = [news_fingerprint(fresh)]
        out = filter_news([fresh, old, bad], seen, 40, now=now)
        assert [it["title"] for it in out] == ["C"], "已读/超窗过滤，解析失败保留"
        assert all("fp" in it for it in out)

    def test_fresh_passes_with_fp(self):
        now = datetime(2026, 11, 17, 10, 0, 0)
        fresh = {"title": "A", "time": "2026-11-17 09:50:00", "source": "em"}
        out = filter_news([fresh], [], 40, now=now)
        assert len(out) == 1 and out[0]["fp"] == news_fingerprint(fresh)


class TestDecide:
    def test_major_neg_sells_and_watches(self):
        pf = _pos(default_portfolio(20000.0), "NVDA")
        _pos(pf, "SPY", qty=65.0, avg=765.0)
        a = _a("NVDA", score=-0.48, major=True, headline="黄仁勋离职")
        st, trades = decide(pf, [a], CFG, NOW)
        assert len(trades) == 1 and trades[0]["kind"] == "sell"
        px = 100.0 * (1 - SLIPPAGE)
        assert trades[0]["symbol"] == "NVDA" and trades[0]["qty"] == 10.0
        assert abs(st["cash"] - (20000.0 + 10.0 * px)) < 1e-6
        assert "NVDA" not in st["positions"]
        assert st["positions"]["SPY"]["qty"] == 65.0, "SPY 底仓冻结"
        w = st["spec"]["watch"]["NVDA"]
        assert w["until"] == (datetime.fromisoformat(NOW)
                              + timedelta(hours=72)).isoformat()
        assert st["spec"]["last_trade"]["NVDA"] == NOW

    def test_spy_never_traded(self):
        pf = _pos(default_portfolio(20000.0), "SPY", qty=65.0, avg=765.0)
        st, trades = decide(pf, [_a("SPY", score=-0.9, major=True)], CFG, NOW)
        assert trades == [] and st["positions"]["SPY"]["qty"] == 65.0
        # 空仓时 SPY 也不买
        st2, trades2 = decide(default_portfolio(20000.0),
                              [_a("SPY", score=0.9, major=True)], CFG, NOW)
        assert trades2 == [] and "SPY" not in st2["positions"]

    def test_major_neg_crash_dip_buy(self):
        # 黄仁勋离职 → NVDA 暴跌26% → 重点观察 + 按跌幅投入20%现金低吸
        pf = default_portfolio(20000.0)
        a = _a("NVDA", score=-0.48, major=True, drop=0.26,
               tier={"drop": 0.25, "cash_pct": 0.20}, headline="黄仁勋离职")
        st, trades = decide(pf, [a], CFG, NOW)
        assert len(trades) == 1 and trades[0]["kind"] == "buy"
        px = 100.0 * (1 + SLIPPAGE)
        assert trades[0]["qty"] == round(20000.0 * 0.20 / px, 4)
        assert "NVDA" in st["spec"]["watch"]

    def test_watch_period_dip_buy(self):
        pf = default_portfolio(20000.0)
        pf["spec"]["watch"]["NVDA"] = {"until": "2026-11-19T00:00:00+00:00",
                                       "headline": "x"}
        a = _a("NVDA", score=0.0, drop=0.16, tier={"drop": 0.15, "cash_pct": 0.15})
        st, trades = decide(pf, [a], CFG, NOW)
        assert len(trades) == 1 and trades[0]["kind"] == "buy"
        px = 100.0 * (1 + SLIPPAGE)
        qty = round(20000.0 * 0.15 / px, 4)
        assert trades[0]["qty"] == qty
        assert abs(st["cash"] - (20000.0 - qty * px)) < 1e-6
        assert st["positions"]["NVDA"]["avg_price"] == px

    def test_score_buy_without_watch(self):
        _, trades = decide(default_portfolio(20000.0),
                           [_a("TSLA", score=0.5)], CFG, NOW)
        assert len(trades) == 1 and trades[0]["kind"] == "buy"

    def test_no_tier_no_buy(self):
        a = _a("TSLA", score=0.5)
        a["tier"] = None
        _, trades = decide(default_portfolio(20000.0), [a], CFG, NOW)
        assert trades == [], "跌幅未达分级不买入"

    def test_cooldown_blocks_buy(self):
        pf = default_portfolio(20000.0)
        pf["spec"]["last_trade"]["NVDA"] = "2026-11-17T01:30:00+00:00"  # 30分钟前
        _, trades = decide(pf, [_a("NVDA", score=0.5)], CFG, NOW)
        assert trades == []

    def test_max_positions_blocks_buy(self):
        pf = default_portfolio(20000.0)
        for sym in ("NVDA", "TSLA", "AAPL"):
            _pos(pf, sym, qty=1.0)
        _pos(pf, "SPY", qty=65.0, avg=765.0)
        _, trades = decide(pf, [_a("MSFT", score=0.5)], CFG, NOW)
        assert trades == [], "投机持仓已满3个（SPY不计入）"

    def test_min_trade_filter(self):
        a = _a("TSLA", score=0.5)                    # 10%现金=100 < 500
        _, trades = decide(default_portfolio(1000.0), [a], CFG, NOW)
        assert trades == []
        _, trades2 = decide(default_portfolio(400.0), [a], CFG, NOW)
        assert trades2 == [], "现金低于单笔最小金额不动作"

    def test_watch_expired_cleaned(self):
        pf = default_portfolio(20000.0)
        pf["spec"]["watch"]["NVDA"] = {"until": "2026-11-16T00:00:00+00:00",
                                       "headline": "x"}
        st, trades = decide(pf, [_a("NVDA", score=0.0)], CFG, NOW)
        assert trades == [] and "NVDA" not in st["spec"]["watch"]

    def test_sell_score_liquidates(self):
        pf = _pos(default_portfolio(20000.0), "NVDA")
        _, trades = decide(pf, [_a("NVDA", score=-0.7)], CFG, NOW)
        assert len(trades) == 1 and trades[0]["kind"] == "sell"
        assert "综合分" in trades[0]["reason"]

    def test_deepcopy_input_unchanged(self):
        pf = _pos(default_portfolio(20000.0), "NVDA")
        before = copy.deepcopy(pf)
        decide(pf, [_a("NVDA", score=-0.48, major=True)], CFG, NOW)
        assert pf == before, "decide 不得修改入参"

    def test_major_positive_event_buy_without_dip(self):
        # 重大利好（LLM≥0.6）即便未深回调也允许小仓位事件买入
        pf = default_portfolio(20000.0)
        a = _a("NVDA", score=0.42, drop=0.02, headline="黄仁勋回归英伟达")
        a["tier"] = None
        a["llm_used"] = True
        a["news_score"] = 0.7
        st, trades = decide(pf, [a], CFG, NOW)
        assert len(trades) == 1 and trades[0]["kind"] == "buy"
        px = 100.0 * (1 + SLIPPAGE)
        assert trades[0]["qty"] == round(20000.0 * 0.08 / px, 4)  # event_buy_cash_pct 0.08
        assert "重大利好事件买入" in trades[0]["reason"]
        assert "NVDA" in st["positions"]

    def test_no_chase_on_mild_positive_without_dip(self):
        # 普通利好（LLM<0.6）且无回调 → 不追高
        pf = default_portfolio(20000.0)
        a = _a("NVDA", score=0.3, drop=0.02)
        a["tier"] = None
        a["llm_used"] = True
        a["news_score"] = 0.5
        _, trades = decide(pf, [a], CFG, NOW)
        assert trades == [], "非重大利好且无回调不追高"

    def test_positive_no_llm_no_tier_no_buy(self):
        pf = default_portfolio(20000.0)
        a = _a("NVDA", score=0.5, drop=0.02)   # 无 llm_used
        a["tier"] = None
        _, trades = decide(pf, [a], CFG, NOW)
        assert trades == [], "未深回调不买"

    def test_positive_event_with_dip_buys(self):
        # 利好 + 已深回调 → 正常分级低吸（利好提升意愿，跌幅决定出手）
        pf = default_portfolio(20000.0)
        a = _a("NVDA", score=0.4, drop=0.16, headline="黄仁勋回归英伟达")
        a["llm_used"] = True
        a["news_score"] = 0.7
        a["tier"] = {"drop": 0.15, "cash_pct": 0.15}
        _, trades = decide(pf, [a], CFG, NOW)
        assert len(trades) == 1 and trades[0]["kind"] == "buy"
        assert "回调分级低吸" in trades[0]["reason"]


# ---------------------------------------------------------------- 突发紧急度

class TestNewsUrgency:
    """news_urgency：突发标记 + 新鲜度 + 严重性 + 情绪强度。"""

    def test_freshness_tiers(self):
        assert news_urgency("英伟达召开股东大会", age_min=5) == 0.25
        assert news_urgency("英伟达召开股东大会", age_min=25) == 0.15
        assert news_urgency("英伟达召开股东大会", age_min=60) == 0.05
        assert news_urgency("英伟达召开股东大会", age_min=121) == 0.0
        assert news_urgency("英伟达召开股东大会", age_min=None) == 0.0

    def test_breaking_marker(self):
        assert news_urgency("突发：英伟达召开股东大会", age_min=5) == 0.55
        assert news_urgency("BREAKING: Nvidia news", age_min=5) == 0.55

    def test_major_and_severe(self):
        # 重大利空 +0.35，情绪分绝对值≥0.5 再 +0.10
        assert news_urgency("黄仁勋离职", age_min=None, major_neg=True) == 0.45
        # 暴跌：严重性 +0.10，规则分 -0.5 +0.10
        assert news_urgency("英伟达暴跌", age_min=3) == 0.45
        # 闪崩只算严重性（不在规则词典里）
        assert news_urgency("英伟达闪崩", age_min=3) == 0.35

    def test_clamp_at_one(self):
        assert news_urgency("突发：黄仁勋离职", age_min=3, major_neg=True) == 1.0


class TestDenialGuard:
    """辟谣/否认类快讯不按重大利空处理，防止突发误抛售。"""

    def test_is_denial(self):
        assert is_denial("辟谣：黄仁勋离职传闻不实") is True
        assert is_denial("CEO denies resignation report") is True
        assert is_denial("黄仁勋离职") is False

    def test_denial_not_major_neg(self):
        heads = [{"title": "辟谣：黄仁勋离职传闻不实", "body": "",
                  "time": "2026-11-17 09:58:00", "fp": "x"}]
        a = assess("NVDA", heads, _intra(), _candles(), CFG, now=BJ_NOW)
        assert a["major_neg"] is False, "辟谣不应触发抛售"
        assert a["news_score"] == -0.5, "仅按规则词计分，不强制 -0.8"
        assert a["urgent"] is False


class TestAssessUrgency:
    """assess 记录紧急度、最急头条与快讯年龄。"""

    def test_urgent_breaking_news(self):
        heads = [{"title": "突发：黄仁勋离职", "body": "",
                  "time": "2026-11-17 09:58:00", "fp": "x"}]
        a = assess("NVDA", heads, _intra(), _candles(), CFG, now=BJ_NOW)
        assert a["urgency"] == 1.0 and a["urgent"] is True
        assert a["news_age_min"] == 2.0
        assert "黄仁勋离职" in a["headline"]
        assert a["major_neg"] is True

    def test_old_major_news_not_urgent(self):
        heads = [{"title": "英伟达CEO黄仁勋离职", "body": "",
                  "time": "2026-11-17 09:00:00", "fp": "x"}]
        a = assess("NVDA", heads, _intra(), _candles(), CFG, now=BJ_NOW)
        assert a["urgency"] == 0.50 and a["urgent"] is False
        assert a["major_neg"] is True, "过期仍属重大利空，只是不抢实时"

    def test_major_headline_wins_over_breaking(self):
        heads = [{"title": "快讯：英伟达发布新品", "body": "",
                  "time": "2026-11-17 09:59:00", "fp": "p"},
                 {"title": "英伟达CEO黄仁勋离职", "body": "",
                  "time": "2026-11-17 09:30:00", "fp": "n"}]
        a = assess("NVDA", heads, _intra(), _candles(), CFG, now=BJ_NOW)
        assert "黄仁勋离职" in a["headline"], "实际事件优先于普通快讯"
        assert a["urgency"] == 0.65 and a["urgent"] is True
        assert len(a["headlines"]) == 2


# ---------------------------------------------------------------- 突发快车道 / 超窗保留

class TestUrgentScan:
    """urgent_headlines + need_full_run：快车道判定。"""

    def test_urgent_hit(self):
        items = [{"title": "突发：黄仁勋离职", "body": "",
                  "time": "2026-11-17 09:57:00", "source": "em", "fp": "a"}]
        out = urgent_headlines(items, CFG, BJ_NOW)
        assert len(out) == 1
        assert out[0]["symbol"] == "NVDA" and out[0]["major_neg"] is True
        assert out[0]["urgency"] == 1.0

    def test_mild_news_not_urgent(self):
        items = [{"title": "英伟达发布新品", "body": "",
                  "time": "2026-11-17 09:40:00", "source": "em", "fp": "a"}]
        assert urgent_headlines(items, CFG, BJ_NOW) == []

    def test_need_full_run(self):
        assert need_full_run([], {"NVDA"}) is False
        assert need_full_run([{"symbol": "NVDA", "major_neg": True}], set()) is True
        assert need_full_run([{"symbol": "NVDA", "major_neg": False}], {"NVDA"}) is True
        assert need_full_run([{"symbol": "NVDA", "major_neg": False}], {"TSLA"}) is False


class TestFilterNewsUrgent:
    """突发保留：超常规窗口的重要快讯仍纳入，普通旧闻丢弃。"""

    def test_urgent_older_kept(self):
        items = [{"title": "突发：英伟达CEO黄仁勋离职", "time": "2026-11-17 09:00:00",
                  "source": "em"},
                 {"title": "英伟达发布新品", "time": "2026-11-17 09:00:00",
                  "source": "em"},
                 {"title": "突发：英伟达暴跌", "time": "2026-11-17 07:00:00",
                  "source": "em"}]
        out = filter_news(items, [], 40, now=BJ_NOW, urgent_lookback_min=120)
        assert [it["title"] for it in out] == ["突发：英伟达CEO黄仁勋离职"]

    def test_urgent_disabled_drops(self):
        items = [{"title": "突发：黄仁勋离职", "time": "2026-11-17 09:00:00",
                  "source": "em"}]
        assert filter_news(items, [], 40, now=BJ_NOW) == []


class TestSortSymbols:
    """评估顺序：紧急标的先抓行情，其余保持原顺序。"""

    def test_urgent_first_stable(self):
        by_sym = {"NVDA": [{"title": "突发：黄仁勋离职", "time": "2026-11-17 09:59:00"}],
                  "AAPL": [{"title": "苹果发布新品", "time": "2026-11-17 09:59:00"}],
                  "TSLA": [], "MSFT": []}
        order = sort_symbols(["TSLA", "AAPL", "NVDA", "MSFT"], by_sym, CFG, BJ_NOW)
        assert order == ["NVDA", "AAPL", "TSLA", "MSFT"]


# ---------------------------------------------------------------- LLM 候选与预算

class TestLlmCandidates:
    """候选范围与优先级：突发 > 重大利空 > 强情绪。"""

    def test_filter_and_order(self):
        weak = _a("AAPL", score=0.2)
        strong = _a("TSLA", score=0.55)
        major = _a("NVDA", score=-0.48, major=True)
        urgent = _a("MSFT", score=0.1, urgent=True, urgency=0.9)
        cands = llm_candidates([weak, strong, major, urgent], CFG)
        # 凡有快讯都送 LLM；重大利空优先 > 突发 > 其余（同龄按输入序）
        assert [c["symbol"] for c in cands] == ["NVDA", "MSFT", "AAPL", "TSLA"]

    def test_positive_event_is_candidate(self):
        # 利好事件关键词分≈0 也要送 LLM（高管回归/新模型），否则永不触发买入
        a = _a("NVDA", score=0.0, headline="黄仁勋回归英伟达")
        assert [c["symbol"] for c in llm_candidates([a], CFG)] == ["NVDA"]

    def test_no_headline_excluded(self):
        assert llm_candidates([_a("GOOGL", score=0.9, headline="")], CFG) == []

    def test_major_neg_outside_weight(self):
        # 综合分不够但 major_neg 仍纳入（需要 LLM 复核）
        cands = llm_candidates([_a("NVDA", score=-0.1, major=True)], CFG)
        assert [c["symbol"] for c in cands] == ["NVDA"]


class _Resp:
    def __init__(self, text):
        self._text = text

    def json(self):
        return {"choices": [{"message": {"content": self._text}}]}


def _post(text, calls=None):
    def f(url, headers=None, json=None, timeout=None):
        if calls is not None:
            calls.append(json)
        return _Resp(text)
    return f


@pytest.fixture
def llm_on(monkeypatch):
    from common.llm_config import LLMConfig
    monkeypatch.setattr("common.llm_config.load_llm_config",
                        lambda *a, **k: LLMConfig(api_key="test-key"))


class TestEnhanceWithLlm:
    """LLM 增强：候选扩展 / 预算优先级 / 利空纪律 / 失败降级。"""

    def test_strong_non_major_gets_llm_score(self, llm_on):
        a = _a("TSLA", score=0.5)
        n = enhance_with_llm([a], CFG, http_post=_post("-0.4"))
        assert n == 1
        assert a["news_score"] == -0.4
        assert abs(a["score"] - (-0.24)) < 1e-9
        assert a["llm_used"] is True

    def test_major_neg_floor_kept(self, llm_on):
        a = _a("NVDA", score=-0.48, major=True)
        enhance_with_llm([a], CFG, http_post=_post("-0.2"))
        assert a["news_score"] == -0.8, "LLM 不能减弱重大利空的抛售纪律"
        assert abs(a["score"] - (-0.48)) < 1e-9

    def test_major_neg_can_deepen(self, llm_on):
        a = _a("NVDA", score=-0.48, major=True)
        enhance_with_llm([a], CFG, http_post=_post("-0.95"))
        assert a["news_score"] == -0.95
        assert abs(a["score"] - (-0.57)) < 1e-9

    def test_budget_and_priority(self, llm_on):
        cfgb = {**CFG, "llm_max_calls": 2}
        urgent = _a("MSFT", score=0.1, urgent=True, urgency=0.9)
        major = _a("NVDA", score=-0.48, major=True)
        strong = _a("TSLA", score=0.9)
        calls = []
        n = enhance_with_llm([strong, major, urgent], cfgb, http_post=_post("0.2", calls))
        assert n == 2 and len(calls) == 2, "预算上限生效"
        contents = [c["messages"][1]["content"] for c in calls]
        assert "NVDA" in contents[0], "重大利空安全复核优先"
        assert "MSFT" in contents[1], "其次突发紧急"
        assert strong["news_score"] == 0.9, "预算耗尽不再调用"

    def test_llm_unavailable_skips(self, monkeypatch):
        from common.llm_config import LLMConfig
        monkeypatch.setattr("common.llm_config.load_llm_config",
                            lambda *a, **k: LLMConfig(api_key=None))
        a = _a("TSLA", score=0.5)
        assert enhance_with_llm([a], CFG, http_post=_post("0.9")) == 0
        assert a["news_score"] == 0.5

    def test_failure_degrades(self, llm_on):
        def boom(*a, **k):
            raise RuntimeError("network down")

        a = _a("TSLA", score=0.5)
        assert enhance_with_llm([a], CFG, http_post=boom) == 0
        assert a["news_score"] == 0.5, "失败静默降级规则分"
        assert "llm_used" not in a

    def test_no_headline_untouched(self, llm_on):
        a = _a("AAPL", score=0.2, headline="")
        assert enhance_with_llm([a], CFG, http_post=_post("0.9")) == 0
        assert a["news_score"] == 0.2

    def test_prompt_includes_all_headlines(self, llm_on):
        a = _a("NVDA", score=-0.48, major=True)
        a["headlines"] = ["突发：黄仁勋离职", "英伟达盘后跳水"]
        calls = []
        enhance_with_llm([a], CFG, http_post=_post("-0.9", calls))
        content = calls[0]["messages"][1]["content"]
        assert "黄仁勋离职" in content and "盘后跳水" in content
        assert "2. " in content, "多条快讯按序号拼进提示词"

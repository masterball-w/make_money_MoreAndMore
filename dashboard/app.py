"""Streamlit 监控面板 —— 实时 K 线图 / 决策流水 / 成交记录（可选依赖）。

运行: streamlit run dashboard/app.py
数据源: data/audit.db（K 线流 + 决策审计日志）
K 线图: plotly 蜡烛图 + 布林带 + 成交点标记（pip install plotly）
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import time

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from decision_layer.audit import AuditLog  # noqa: E402
from knowledge_layer import indicators  # noqa: E402

st.set_page_config(page_title="交易系统监控", page_icon="📊", layout="wide")

# 自动刷新（动态快照）：勾选后每 N 秒重新拉取审计库
with st.sidebar:
    st.title("⚙️ 监控选项")
    auto = st.toggle("自动刷新（动态快照）", value=True)
    interval = st.select_slider("刷新间隔(秒)", options=[5, 10, 15, 30, 60], value=10)
    st.divider()
    st.caption("数据源: data/audit.db")

st.title("📊 自动化交易系统 — 实时监控")

DB = ROOT / "data" / "audit.db"
if not DB.exists():
    st.warning("尚未生成审计数据库：先运行 python main.py（paper 模式）产生数据。")
    st.stop()

audit = AuditLog(DB)

# ---------------------------------------------------------------- 实时 K 线图
klines = audit.recent_klines("BTC/USDT", 150)
if klines:
    df = pd.DataFrame(klines)
    df["ts"] = pd.to_datetime(df["ts"])
    closes = df["close"].tolist()
    mid, upper, lower = indicators.bollinger(closes, 20, 2.0)
    df["boll_mid"] = mid
    df["boll_up"] = upper
    df["boll_low"] = lower

    st.subheader("实时 K 线（含布林带）")
    try:
        import plotly.graph_objects as go
        fig = go.Figure()
        fig.add_trace(go.Candlestick(
            x=df["ts"], open=df["open"], high=df["high"], low=df["low"],
            close=df["close"], name="K线",
            increasing_line_color="#26a69a", decreasing_line_color="#ef5350"))
        fig.add_trace(go.Scatter(x=df["ts"], y=df["boll_mid"], name="布林中轨",
                                 line=dict(color="#888", width=1)))
        fig.add_trace(go.Scatter(x=df["ts"], y=df["boll_up"], name="布林上轨",
                                 line=dict(color="#2962ff", dash="dot", width=1)))
        fig.add_trace(go.Scatter(x=df["ts"], y=df["boll_low"], name="布林下轨",
                                 line=dict(color="#2962ff", dash="dot", width=1)))
        # 成交点标记
        fills = pd.DataFrame(audit.recent_fills(200))
        if not fills.empty:
            fills["ts"] = pd.to_datetime(fills["ts"])
            merged = pd.merge_asof(
                fills.sort_values("ts"), df[["ts", "close"]].sort_values("ts"),
                on="ts", direction="nearest")
            buys = merged[merged["side"] == "buy"]
            sells = merged[merged["side"] == "sell"]
            if not buys.empty:
                fig.add_trace(go.Scatter(
                    x=buys["ts"], y=buys["close"], mode="markers", name="买入",
                    marker=dict(symbol="triangle-up", size=13, color="#26a69a")))
            if not sells.empty:
                fig.add_trace(go.Scatter(
                    x=sells["ts"], y=sells["close"], mode="markers", name="卖出",
                    marker=dict(symbol="triangle-down", size=13, color="#ef5350")))
        fig.update_layout(height=460, xaxis_rangeslider_visible=False,
                          margin=dict(l=10, r=10, t=10, b=10))
        st.plotly_chart(fig, use_container_width=True)
    except ImportError:
        st.info("安装 plotly 后显示蜡烛图: pip install plotly")
        st.dataframe(df[["ts", "open", "high", "low", "close", "volume"]].tail(60),
                     use_container_width=True)

# ---------------------------------------------------------------- 决策流水
decisions = pd.DataFrame(audit.recent_decisions(200))
fills = pd.DataFrame(audit.recent_fills(200))

left, mid, right = st.columns(3)
left.metric("决策总数", len(decisions))
mid.metric("成交总数", len(fills))
if not decisions.empty:
    right.metric("被风控拦截", int(decisions["blocked_by_risk"].sum()))

st.subheader("最近决策流水")
if not decisions.empty:
    st.dataframe(
        decisions[["ts", "action", "symbol", "quantity", "reference_price",
                   "llm_sentiment", "quant_signal", "confidence", "reason"]],
        use_container_width=True, height=320)

st.subheader("最近成交")
if not fills.empty:
    st.dataframe(fills, use_container_width=True, height=260)

st.caption("四层链路: 信息层(新闻+实时K线+资金流) → 决策层(突破+资金流检测+风控门禁) → 操作层 → 成交回流。审计库: data/audit.db")
audit.close()

# 自动刷新放在脚本末尾（渲染完成后再等待，避免空白循环）
if auto:
    time.sleep(interval)
    st.rerun()

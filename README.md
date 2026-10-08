# 动态决策自动化交易系统

四层事件驱动架构：**实时信息层 → 决策层 → 操作层**，外加贯穿全局的**底层逻辑计算层**。
当前为阶段1交付物：模拟盘（paper）全链路已跑通，实盘接口已预留但默认锁定。

> ⚠️ **风险声明**：没有任何系统能保证盈利。新闻驱动交易存在天然滞后性
> （新闻到达时机构算法往往已完成交易）。本系统的设计哲学是：
> **风控优先、可解释、可回测、可降级**。回测不达标不得进入实盘（门禁在流程里，不在口头）。

---

## 一、架构总览

```
┌────────────────────────────────────────────────────────────────┐
│  ① 实时信息层  info_layer                                        │
│  新闻源适配器(美联储RSS/地缘/AI大模型/加密/国内财经)                  │
│  实时K线流 OHLCV(ccxt/Mock, 盘中tick+收盘)                         │
│  新闻: 抓取→指纹去重→P1/P2/P3分级→标的关联                          │
└──────────────┬─────────────────────────────────────────────────┘
               │ NewsEvent / KlineEvent / MarketEvent
┌──────────────▼─────────────────────────────────────────────────┐
│  ② 决策层  decision_layer                                       │
│  突破检测器: MACD金叉死叉/布林带穿越/Donchian高低点突破               │
│      → 实时K线指标穿越阈值 → SignalEvent → 立即强制评估              │
│  LLM分析器(情绪/事件解读, 只出观点不出订单) + 量化引擎(技术信号)       │
│  三路融合: score = w_llm·情绪 + w_quant·量化 + w_breakout·突破      │
│  + 经济事件日历(事件窗口降仓) + 风险管理器(六项门禁) + 审计日志        │
└──────────────┬─────────────────────────────────────────────────┘
               │ DecisionEvent（已过风控门禁）
┌──────────────▼─────────────────────────────────────────────────┐
│  ③ 操作层  execution_layer                                      │
│  Broker抽象: PaperBroker(模拟) / CryptoBroker(ccxt) / A股(QMT)    │
│  幂等下单 → 成交回报 FillEvent → 回流决策层                        │
└─────────────────────────────────────────────────────────────────┘
               ▲ 知识查询（公式/指标/术语/市场规则）
┌──────────────┴──────────────────────────────────────────────────┐
│  ④ 底层逻辑计算层  knowledge_layer（纯函数，无状态无IO）            │
│  风险公式(VaR/波动率/回撤/凯利) + 技术指标 + 术语词典 + 市场规则      │
└─────────────────────────────────────────────────────────────────┘
```

四层之间**只通过事件通信**（`common/events.py` 定义、`common/bus.py` 传输），
互相不 import 对方实现 —— 任何一层可以独立替换、测试、回放。

### 技术突破驱动交易（K 线 → 指标 → 操作）

信息层推送实时 K 线流（`KlineEvent`：当前未收盘 K 线 + 已收盘历史），
决策层的 `BreakoutDetector` 对每次更新计算指标并做**穿越检测**：

| 信号 | 触发条件 | 含义 |
|---|---|---|
| `macd_golden` / `macd_death` | DIF 上穿/下穿 DEA | 动量反转 |
| `boll_up` / `boll_down` | 收盘价上穿/下穿布林带轨道 | 波动率突破 |
| `donch_up` / `donch_down` | 突破/跌破前 20 根 K 线高/低点 | 海龟式趋势入场 |

触发是"穿越"而非"处于"（避免贴轨重复报）；同一根 K 线同类信号去抖；
多信号同时触发自动合并。信号强度 = 突破幅度 / ATR（相对波动归一化）。
突破信号直接**强制触发**决策评估（不等下一个行情周期），与 LLM 情绪、
量化信号三路加权融合后过风控门禁下单。信号有效期 15 分钟线性衰减。

## 二、目录结构

```
trading-system/
├── main.py                  # 阶段1演示入口（全 Mock，python main.py）
├── real_run.py              # 真实试运行入口（真实行情+新闻，paper 撮合）
├── pyproject.toml           # 依赖全部可选，核心零第三方依赖
├── config/
│   ├── settings.yaml        # 模式(paper/live)/信息源/LLM 配置
│   ├── risk_limits.yaml     # 风控阈值、策略参数、突破检测参数
│   ├── economic_calendar.yaml   # 经济事件日历（事件窗口降仓）
│   └── knowledge/
│       ├── glossary.yaml    # 金融术语词典（供 LLM 上下文注入）
│       └── market_rules.yaml# 两市场制度差异（T+1/手续费/最小单位）
├── common/                  # 事件定义 + 消息总线 + 网络(重试) + 账户/订单模型
├── info_layer/              # ① 实时信息层
│   ├── base.py              #   NewsSource 抽象接口
│   ├── sources/             #   google_news / rss / cryptopanic / mock 适配器
│   ├── market_data.py       #   行情快照（Mock / ccxt / akshare）
│   ├── kline.py             #   实时K线流 OHLCV（Mock tick+收盘 / ccxt）
│   ├── rest_kline.py        #   真实K线 REST源（OKX主/Binance备，故障切换+退避）
│   ├── pipeline.py          #   去重(指纹) + 分级 + 标的关联 + 时效过滤
│   └── aggregator.py        #   轮询编排（新闻/行情/K线三循环）+ 发布事件
├── knowledge_layer/         # ④ 底层逻辑计算层
│   ├── formulas/risk.py     #   VaR/波动率/回撤/夏普/凯利/风险仓位
│   ├── indicators.py        #   SMA/EMA/MACD/RSI/布林带/ATR
│   ├── glossary.py          #   术语服务
│   ├── market_rules.py      #   市场规则服务
│   └── knowledge_service.py #   统一门面
├── decision_layer/          # ② 决策层
│   ├── breakout_detector.py #   指标突破检测（MACD/布林/Donchian 穿越→SignalEvent）
│   ├── llm_analyzer.py      #   LLM 分析（含关键词降级模式）
│   ├── quant_engine.py      #   技术信号引擎
│   ├── event_calendar.py    #   经济日历 + 事件窗口
│   ├── risk_manager.py      #   风控门禁（R1~R6，见下）
│   ├── strategy.py          #   决策编排（三路信号融合/止损/产出决策）
│   └── audit.py             #   SQLite 审计日志（决策/K线/成交）
├── execution_layer/         # ③ 操作层
│   ├── brokers/             #   paper / crypto(ccxt) / cn_stock
│   ├── executor.py          #   幂等订单执行
│   └── reconciler.py        #   定期对账
├── backtest/replay.py       # 事件回放回测（与实盘同一套代码）
├── dashboard/app.py         # Streamlit 监控面板（实时K线蜡烛图）
└── tests/                   # 单元测试（59 项）
```

## 三、快速开始

```bash
cd trading-system
pip install pyyaml feedparser httpx    # 真实模式依赖（演示 main.py 只需 pyyaml）

python main.py                         # 阶段1演示：Mock 全链路闭环（确定性剧本）
python real_run.py --duration 600      # 真实试运行：OKX/Binance真实K线 + 真实新闻，paper 撮合
python -m pytest tests/ -q             # 单元测试（59 项）

# 可选
export OPENAI_API_KEY=sk-...           # 启用真实 LLM 新闻分析（不设则关键词模式）
pip install streamlit pandas plotly    # 监控面板（含实时K线蜡烛图）
streamlit run dashboard/app.py
```

### 真实试运行（real_run.py）

接入的真实数据源（行情均无需 API key）：

| 数据 | 源 | 说明 |
|---|---|---|
| K 线 | OKX → Binance 主备 | 公开 REST，8s 轮询，失败自动切换 + 指数退避 |
| 美联储/央行 | Fed 官网 RSS | FOMC 声明/纪要，权威一手 |
| 综合快讯 | Google News RSS × 6 主题 | 美联储/中东战争/央行/AI大模型/加密关键词订阅 |
| 加密行业 | Cointelegraph RSS | 行业动态 |

已实测验证：301 秒试运行处理 117 条真实新闻，以真实价格（BTC 83189）完成
情绪驱动买入，风控矛盾检测（美联储加息新闻 vs 量化看多 → HOLD）与
冷却期拦截（8 次重复开仓全拦）均按设计工作；期间 OKX 一次故障自动切至 Binance。

## 四、风控体系（决策层的守门员）

所有 `DecisionEvent` 必须过 `RiskManager` 六项检查才能到达操作层：

| 编号 | 规则 | 默认阈值 |
|---|---|---|
| R1 | 单笔风险 ≤ 净值比例（超限自动削减数量而非拒单） | 1% |
| R2 | 单标的市值 ≤ 净值比例 | 20% |
| R3 | 账户回撤 ≥ 阈值 → **全局熔断**，禁止一切开仓，需人工解锁 | 10% |
| R4 | 重大经济事件公布前 N 分钟 → 新开仓数量×0.5 | 60 分钟 |
| R5 | 同标的冷却期（防新闻反复触发） | 10 分钟 |
| R6 | 现金充足检查 | — |

另有两条硬规则内建在策略层：
- **止损优先**：持仓浮亏超 5% 无条件平仓，不依赖任何信号；
- **信号矛盾降级**：LLM 与量化方向相反且都强 → HOLD（保守优先）。

系统级降级：LLM 故障 → 关键词模式；信息源连续失败 → 告警；行情中断 → 不决策。

## 五、决策可解释性（审计）

每笔决策写入 `data/audit.db`，包含完整输入快照：
触发新闻 ID、LLM 情绪分与推理原文、量化信号明细、风控六项检查逐条结果、
最终是否被削减/拦截。`dashboard/` 可视化，`audit.recent_decisions()` 可编程复盘。

## 六、实施路线与当前状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| 1 | 骨架 + 模拟盘全链路闭环（本仓库当前交付物） | ✅ 完成 |
| 2 | 真实信息源接入（RSS/CryptoPanic/财联社）+ ccxt/akshare 实时行情 | 🔜 接口已预留 |
| 3 | 底层逻辑层扩展（更多指标/公式 + 单测覆盖） | ✅ 核心已含 |
| 4 | LLM 实测调优 + 经济日历 API 接入 | 🔜 |
| 5 | 历史数据回测验证（backtest/replay.py） | 🔜 框架已含 |
| 6 | 小资金实盘（先加密后A股；A股需券商 QMT/恒生通道） | 🔒 双重确认锁定 |

**实盘解锁条件**：`TRADING_MODE=live` + `TRADING_LIVE_CONFIRM=YES` 两个环境变量
同时设置，且建议仅在回测夏普 > 1、最大回撤 < 10%、模拟盘连续盈利 4 周后进行。

## 七、合规提示

- A股程序化交易须遵守交易所报备规定，本系统只做**低频**（小时级/日级）决策；
- 个人使用加密交易所 API 须遵守当地法规与交易所条款；
- 本项目仅供学习研究，不构成投资建议，据此交易风险自负。

# LLM 分析 API 接入教程（智谱 AI BigModel / 任意 OpenAI 兼容服务）

本教程说明如何把 AI 大模型分析能力接入本金融交易系统，以及系统与 AI 之间的完整接口定义。

**当前已接入**：智谱 AI BigModel **编程套餐（Coding Plan）端点**，密钥存放于 `config/api.txt`，
默认模型 `glm-5.3`（旗舰），均已实测可用。

---

## 一、接口总览

系统通过 **OpenAI 兼容协议**（Chat Completions）与 AI 服务通信，
因此任何兼容该协议的服务（智谱 GLM / OpenAI / DeepSeek / Moonshot / 自建网关）
只需修改 `base_url` 和 `model` 两项即可接入。

### 1.1 HTTP 接口定义

| 项目 | 值 |
|---|---|
| 端点 URL | `https://open.bigmodel.cn/api/coding/paas/v4/chat/completions` |
| 认证方式 | `Authorization: Bearer <API_KEY>` |
| 请求格式 | JSON（UTF-8） |
| 响应格式 | JSON（OpenAI Chat Completion 结构） |

> **⚠️ 端点区别（智谱密钥最容易踩的坑）**：
> - `/api/coding/paas/v4/` —— 走**编程套餐（Coding Plan）额度**，可用 GLM-5.3 全系列
> - `/api/paas/v4/` —— 走**账户充值余额**，编程套餐密钥在此端点调用付费模型会报
>   `429 余额不足`（免费档 glm-4.5-flash 除外）
>
> 同一个密钥两个端点都认识，但额度池完全独立。SDK 用法一致，只差 base_url 一个词。

OpenAI SDK 用户把 `base_url` 设为 `https://open.bigmodel.cn/api/coding/paas/v4/` 即可，
其余调用方式与 OpenAI 完全一致。

### 1.2 模型实测矩阵（本密钥编程套餐，2026-10 实测）

| 模型 | 状态 | 延迟（关思考） | 说明 |
|---|---|---|---|
| `glm-5.3` | ✅ **可用（当前默认）** | ~2.4s | GLM-5 旗舰，情绪判断质量最佳 |
| `glm-5.3-flash` | ✅ 可用 | ~2.0s | 低延迟备选，高频调用场景 |
| `glm-5.3-flashx` / `glm-5.2` / `glm-5.1` / `glm-5` / `glm-5-turbo` / `glm-4.5~4.7` | ✅ 端点在售 | — | 改 model 名即用 |
| `glm-4.5-flash` / `glm-4-flash-250414` | ✅ 可用（通用端点免费档） | — | 不消耗任何额度，可作降级兜底 |

`thinking` 开关对 GLM-5.3 的影响（实测）：开启 11.8s / 关闭 2.4s，
输出质量均达标；交易场景默认关闭以保证时效。
模型清单可随时用 `client.models.list()` 查询。

### 1.3 系统向 AI 发送什么（请求体）

系统每次分析一条新闻，请求体结构如下（见 `decision_layer/llm_analyzer.py`）：

```json
{
  "model": "glm-4.5-flash",
  "temperature": 0.2,
  "thinking": {"type": "disabled"},
  "messages": [
    {
      "role": "system",
      "content": "你是一名宏观与加密市场的新闻分析师。分析新闻对市场的短期影响。只输出 JSON，不要输出其他文字，格式：{\"sentiment\": -1.0~1.0 的浮点数(正=利多风险资产,负=利空), \"symbols\": [\"受影响标的如 BTC/USDT\"], \"horizon_minutes\": 影响持续分钟数(整数), \"confidence\": 0~1, \"reasoning\": \"一句话中文推理\"} 不确定时 sentiment 取 0、confidence 取低值。"
    },
    {
      "role": "user",
      "content": "[知识层注入的术语上下文]\n\n[新闻]\n标题: ...\n正文: ...(截断至800字)\n来源: ... 级别: P1"
    }
  ]
}
```

要点：
- **知识层上下文注入**：发送前 `KnowledgeService` 会把新闻中出现的金融术语
  （`config/knowledge/glossary.yaml`）的释义注入 user 消息，提高分析准确度；
- **`thinking` 参数**是智谱 GLM 4.5+ 专有开关（关闭深度思考以降低延迟），
  其他兼容服务会自动忽略，不影响接入。

### 1.4 AI 需要返回什么（响应体）

系统只读取 `choices[0].message.content`，要求是（可带 ```json 代码围栏）：

```json
{
  "sentiment": 0.8,
  "symbols": ["BTC/USDT"],
  "horizon_minutes": 120,
  "confidence": 0.9,
  "reasoning": "美联储意外大幅降息并重启量化宽松，显著改善市场流动性"
}
```

| 字段 | 类型 | 含义 | 系统内的用途 |
|---|---|---|---|
| `sentiment` | float，-1.0~1.0 | 情绪分（正=利多风险资产） | 四路融合决策的 w_llm 权重项 |
| `symbols` | list[str] | 受影响标的 | 关联到 watchlist 持仓判断 |
| `horizon_minutes` | int | 影响持续分钟数 | 信号有效期（线性衰减） |
| `confidence` | float，0~1 | 置信度 | 与量化信号矛盾时的降级判断 |
| `reasoning` | str | 一句话推理 | 写入审计日志（可解释性） |

**AI 只出观点，绝不直接产出订单**——情绪分还要与量化信号、K线突破、资金流
四路加权融合，并经过风控六项门禁（R1~R6）才能形成交易决策。

---

## 二、三步接入教程

### 第 1 步：放置 API 密钥

把密钥写入 `config/api.txt`（单行纯文本即可）：

```
你的密钥（32位十六进制.16位随机串 的完整字符串）
```

> 该文件已被 `.gitignore` 排除，不会被提交到版本库。
> 也可以不放文件：直接在 `settings.yaml` 的 `llm.api_key` 填密钥，
> 或设置环境变量 `OPENAI_API_KEY`。

### 第 2 步：确认 `config/settings.yaml` 的 llm 段

```yaml
llm:
  api_key: ""                    # 直接填密钥（不推荐明文入库）
  api_key_file: config/api.txt   # 密钥文件（api_key 优先于本文件）
  base_url: https://open.bigmodel.cn/api/paas/v4/   # OpenAI 兼容端点
  model: glm-4.5-flash           # 模型名
  timeout_seconds: 20            # 超时后自动降级关键词模式
  thinking: disabled             # GLM 4.5+ 专用，其他服务自动忽略
```

取值优先级（`common/llm_config.py` 统一实现）：

```
base_url / model :  环境变量 OPENAI_BASE_URL / LLM_MODEL  >  settings.yaml  >  默认值
api_key          :  settings.yaml 的 api_key  >  api_key_file  >  环境变量 OPENAI_API_KEY
```

### 第 3 步：连通性自检

```bash
.venv/Scripts/python.exe scripts/check_llm.py
```

三项全部通过后即可启动系统：

```bash
.venv/Scripts/python.exe real_run.py --duration 600
# 启动日志出现 "LLM 分析器: 已启用(glm-4.5-flash @ https://open.bigmodel.cn/...)"
# 即代表 AI 分析已生效；审计库中 engine 字段为 "llm"
```

---

## 三、更换其他 AI 供应商（示例）

只改 `settings.yaml` 两行，无需改任何代码：

| 供应商 | base_url | 推荐模型 |
|---|---|---|
| 智谱 AI（当前） | `https://open.bigmodel.cn/api/coding/paas/v4/` | `glm-5.3`（编程套餐） |
| 智谱 AI（充值余额） | `https://open.bigmodel.cn/api/paas/v4/` | `glm-4.5-flash` 等全部在售模型 |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| Moonshot | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |

更换后把 `thinking` 留空（该参数仅 GLM 4.5+ 有效），并重跑
`scripts/check_llm.py` 确认结构化输出仍可解析。

---

## 四、系统内部调用链（接入点位置）

```
real_run.py  ──load_llm_config()──>  LLMConfig(api_key/base_url/model/extra_body)
    │
    └─> LLMAnalyzer(knowledge, api_key=..., base_url=..., model=..., extra_body=...)
              │  每条 NewsEvent 触发一次 analyze()
              ├─ 知识层注入术语上下文（KnowledgeService.llm_context_for）
              ├─ AsyncOpenAI(base_url).chat.completions.create(...)
              ├─ JSON 解析（自动剥离代码围栏 + 字段钳位）
              └─ NewsAnalysis(sentiment, symbols, horizon, confidence, reasoning)
                        │
                        └─> StrategyOrchestrator 四路融合 → 风控门禁 → 订单
```

关键代码位置：

| 文件 | 职责 |
|---|---|
| `common/llm_config.py` | **配置加载接口**（本教程的核心接口，新增） |
| `decision_layer/llm_analyzer.py` | AI 调用 + JSON 解析 + 关键词降级 |
| `scripts/check_llm.py` | 连通性自检（新增） |
| `config/settings.yaml` | llm 配置段 |
| `config/api.txt` | 密钥文件（gitignore 保护） |

---

## 五、降级与容错（系统级设计）

| 场景 | 系统行为 |
|---|---|
| 未配置密钥 | 自动降级**关键词情绪模式**（词典打分，可解释） |
| 调用超时 / 网络故障 | 该条新闻降级关键词模式，**系统不停摆** |
| 返回非法 JSON | 同上，并在日志记录异常 |
| 429 余额不足 | 先确认端点与额度池匹配（编程套餐密钥必须走 `/api/coding/paas/v4/`）；套餐额度耗尽时可临时切通用端点 + `glm-4.5-flash` 免费档兜底 |

**安全提示**：密钥等同账户密码。`config/api.txt` 已被 `.gitignore` 排除，
请勿把密钥写入会提交的文件（如 settings.yaml 明文）或截图外发。

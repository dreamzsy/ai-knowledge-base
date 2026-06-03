# CLAUDE.md

本文件为 Claude Code 在本仓库工作时提供项目上下文与协作约束。

## 1. 项目概述

本项目是一个 **AI 知识库助手**，目标是把分散在社区前沿的 AI/LLM/Agent 技术动态，自动沉淀为可检索、可分发的结构化知识。

核心能力：

- **采集**：定时抓取 GitHub Trending 与 Hacker News 中 AI / LLM / Agent 相关条目。
- **分析**：调用大模型对原始内容进行摘要、打标签、价值判断，输出结构化 JSON。
- **存储**：原始数据落 `knowledge/raw/`，经分析后的知识条目落 `knowledge/articles/`。
- **分发**：支持多渠道推送（Telegram、飞书），按主题/标签订阅。

## 2. 技术栈

| 层次 | 选型 | 说明 |
| --- | --- | --- |
| 语言运行时 | Python 3.12 | 统一使用 `match/case`、PEP 695 泛型语法 |
| Agent 框架 | Claude Code + 国产大模型 | Claude Code 负责本地编排，国产大模型承担批量分析任务 |
| 流程编排 | LangGraph | 多 Agent 协作的状态机与条件路由 |
| 采集底座 | OpenClaw | 统一抽象 GitHub Trending / HN 等数据源 |
| 存储 | 本地 JSON 文件 | 以 `knowledge/articles/<id>.json` 为单元 |

## 3. 编码规范

- 严格遵守 **PEP 8**；行宽 100 字符。
- 命名一律 **snake_case**（模块、函数、变量），类名使用 `PascalCase`，常量 `UPPER_SNAKE`。
- 所有公开函数/类必须写 **Google 风格 docstring**，包含 `Args`、`Returns`、`Raises`。
- **禁止裸 `print()`**：日志统一走 `logging.getLogger(__name__)`，调试输出请删除后再提交。
- 类型注解必填：函数签名、模块级常量、复杂数据结构（用 `TypedDict` / `pydantic.BaseModel`）。
- 异常处理只在边界层（IO、网络、外部 API）兜底，内部逻辑让异常向上抛。

## 4. 项目结构

```
ai-knowledge-base/
├── .claude/
│   └── agents/              # Claude Code 子 Agent 定义（采集 / 分析 / 整理）
├── skills/                  # 可复用的 Skill 脚本（抓取、清洗、分发等）
├── knowledge/
│   ├── raw/                 # 原始抓取结果，按来源+日期归档（只追加，不修改）
│   └── articles/            # 经过 AI 分析后的结构化知识条目（每条一个 JSON）
├── src/                     # 主程序代码
└── CLAUDE.md
```

约束：

- `knowledge/raw/` 视为**只读归档**，任何修订必须落到 `knowledge/articles/`。
- `.claude/agents/` 与 `skills/` 中新增文件须有头部说明（用途、输入、输出）。

## 5. 知识条目 JSON 格式

文件位置：`knowledge/articles/<id>.json`，单条记录示例：

```json
{
  "id": "2026-06-03-langgraph-multi-agent",
  "title": "LangGraph 0.3 引入持久化状态图",
  "source": "github_trending",
  "source_url": "https://github.com/langchain-ai/langgraph",
  "author": "langchain-ai",
  "published_at": "2026-06-02T10:00:00+08:00",
  "collected_at": "2026-06-03T08:15:00+08:00",
  "summary": "LangGraph 0.3 新增持久化状态图，支持长会话恢复……",
  "highlights": ["持久化状态", "条件路由", "多 Agent 协作"],
  "tags": ["LLM", "Agent", "LangGraph"],
  "language": "zh",
  "score": 0.86,
  "status": "published",
  "channels": ["telegram", "feishu"]
}
```

字段说明：

- `id`：`YYYY-MM-DD-<slug>`，全局唯一，禁止复用。
- `status`：`draft` / `reviewed` / `published` / `archived`，状态机单向推进。
- `score`：模型给出的价值评分（0–1），低于阈值不进入分发。
- `tags`：使用受控词表，新增标签需在 `knowledge/tags.yaml` 注册。

## 6. Agent 角色概览

| 角色 | 定义位置 | 主要职责 | 输入 | 输出 |
| --- | --- | --- | --- | --- |
| **采集 Agent**（collector） | `.claude/agents/collector.md` | 调度 OpenClaw 抓取 GitHub Trending / Hacker News，原始数据入 `knowledge/raw/` | 数据源配置、时间窗口 | `raw/<source>/<date>/*.json` |
| **分析 Agent**（analyzer） | `.claude/agents/analyzer.md` | 调用大模型生成摘要、亮点、标签、价值评分，产出符合规范的知识条目 | `raw/` 中的待处理记录 | `articles/<id>.json`（`status=draft`） |
| **整理 Agent**（curator） | `.claude/agents/curator.md` | 对 `draft` 条目做去重、合并、人工/自动审校，推进到 `published` 并触发分发 | `status=draft` 的条目 | `status=published` 的条目 + 多渠道推送 |

协作方式：三个 Agent 通过 LangGraph 的状态机串联，状态流转为 `collected → drafted → published`，失败重试与人工介入点定义在 `src/graph.py`。

## 7. 红线（绝对禁止）

以下行为视为高风险操作，**任何情况下都需要先与用户确认，不得自动执行**：

1. **不得删除或覆盖 `knowledge/raw/` 中的任何文件**；原始数据是审计依据。
2. **不得擅自修改已发布条目**（`status=published` / `archived`）；如需更正，必须新增 `revision` 记录而非原地改写。
3. **不得在代码或日志中写入任何密钥**（Telegram Bot Token、飞书 App Secret、模型 API Key）；统一从环境变量或 `.env`（已 gitignore）读取。
4. **不得对外部渠道（Telegram / 飞书）真实发送消息进行调试**；调试时使用 `DRY_RUN=true` 或专用测试群。
5. **不得绕过 `score` 阈值与标签校验**直接分发；分发链路必须经过 curator 审核节点。
6. **不得引入未列入技术栈的重型依赖**（如更换 Agent 框架、新增数据库）；如确有需要，先提案再实施。
7. **不得使用 `git push --force`、`git reset --hard`、`rm -rf` 等破坏性命令**操作本仓库或共享分支。
8. **不得抓取明确声明禁止采集的源**，并须遵守目标站点的 `robots.txt` 与速率限制。

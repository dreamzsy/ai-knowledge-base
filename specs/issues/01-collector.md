# Issue 01 · collector 采集任务票

> 来源：specs/agents-prd.md §3.1 / §5 / §6
> 状态：todo
> 类型：AFK（schema 锁定后可自主实现）

## depends_on

- 无（链路第一棒，可立即开工）
- 决策前置（HITL，建议先拍）：
  - 「AI 相关」过滤判定规则（§6）—— 本票已给默认规则，见 schema 下方«过滤规则»，如不认可需先改。

## What to build

每天 UTC 0:00 触发，抓取 GitHub Trending **Top 50**，过滤出 AI 相关条目，
仅保留**客观事实字段**，逐条落盘到 `knowledge/raw/<date>/<id>.json`（每条一文件，只追加不覆盖）。
本票只交付「采集 → 落盘 raw + 返回清单」这一端到端薄切片，不做任何分析。

## acceptance

- [ ] 能抓取 GitHub Trending Top 50（取不到的字段填 `null`，不臆造）。
- [ ] 按«过滤规则»筛出 AI 相关条目，非 AI 条目被排除。
- [ ] 每条新建 `knowledge/raw/<date>/<id>.json`，已存在同 id 则跳过（不覆盖，守 §7-1）。
- [ ] 落盘内容与返回清单字段一致，且符合下方 output schema。
- [ ] 未写 `raw/` 以外任何路径；未执行破坏性命令；未写入密钥。
- [ ] 返回值为可被 `JSON.parse` 的纯 JSON 数组（无说明文字、无代码围栏）。

## schema

**output**（落盘文件 = 返回数组元素，下游 analyzer 的 input）：

```json
{
  "id": "YYYY-MM-DD-<slug>",
  "source": "github_trending",
  "title": "仓库原始标题",
  "source_url": "https://github.com/owner/repo",
  "author": "owner",
  "programming_language": "主语言（无则 null）",
  "raw_excerpt": "README/描述原文，不改写（无则 null）",
  "raw_metrics": { "stars": 0, "stars_period": null, "forks": 0 },
  "published_at": "ISO8601（无则 null）",
  "collected_at": "ISO8601（采集时刻）",
  "raw_ref": "knowledge/raw/<date>/<id>.json"
}
```

**过滤规则（AI 相关判定，默认值，HITL 可改）**：
- 命中即保留：topics/description/title 含 `ai`/`llm`/`agent`/`rag`/`mcp`/`model`/`ml`/
  `transformer`/`diffusion`/`prompt` 等关键词（大小写无关）。
- 三者全不命中 → 排除。判定规则与命中词记录到日志，便于 review。

**约束**：`source` 仅 `github_trending`；不得含 summary/tags/score 等分析字段；缺失填 `null`。

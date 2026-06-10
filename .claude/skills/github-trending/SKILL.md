---
name: github-trending
description: 当需要采集 GitHub 热门开源项目时使用此技能
allowed-tools: Read, Grep, Glob, WebFetch
---

# GitHub Trending 采集技能

采集 GitHub 上的热门开源项目，聚焦 AI / LLM / Agent 方向，
撰写中文摘要后按热度排序，输出结构化 JSON 归档到 `knowledge/raw/`。

## 使用场景

- 需要拉取最新的 GitHub 热门开源项目动态时。
- 为知识库流水线（analyzer / organizer）准备 AI 领域原始素材时。
- 需要定期（如每日）沉淀技术趋势、生成可检索归档时。

## 执行步骤

1. **搜索热门仓库**：通过 GitHub API（`https://api.github.com/search/repositories`）
   按 star 数、近期热度检索候选仓库，覆盖足够多的候选池（建议 ≥50 条）。
2. **提取信息**：对每个仓库提取 `name`、`url`、`description`、`stars`、`language`、`topics`
   等客观字段；拿不到的字段填 `null`，不臆造。
3. **过滤**：**纳入** AI / LLM / Agent 相关项目（topics/描述/名称命中
   `ai`/`llm`/`agent`/`rag`/`mcp`/`model` 等关键词）；**排除** Awesome 列表类
   仓库（名称或描述以 `awesome` 开头/为主的清单型项目，非实质代码项目）。
4. **去重**：按仓库全名（`owner/repo`）或 `url` 去重，重复项只保留一条。
5. **撰写中文摘要**：为每条写一句中文 `summary`，遵循公式
   **「项目名 + 做什么 + 为什么值得关注」**，客观陈述，不夸大。
6. **排序取 Top 15**：按 star 数（结合近期热度）降序排序，取前 **15** 条。
7. **输出 JSON**：写入 `knowledge/raw/github-trending-YYYY-MM-DD.json`
   （`YYYY-MM-DD` 为采集当日），结构见下方「输出格式」。

## 注意事项

- **只采集客观事实**：不打分、不贴标签、不做价值判断（那是 analyzer 的职责）。
- **不臆造数据**：star 数、语言、topics 等必须来自 API 真实返回，缺失填 `null`。
- **遵守速率限制**：尊重 GitHub API 速率限制与 `robots.txt`，避免高频请求。
- **只追加不覆盖**：`knowledge/raw/` 为只读归档区，同日文件已存在时谨慎处理，
  不得删除或覆盖既有原始数据。
- **不写入密钥**：任何 token / secret 不得出现在输出文件或日志中。
- **摘要语言**：`summary` 统一用中文；其余客观字段保留原文。

## 输出格式

写入 `knowledge/raw/github-trending-YYYY-MM-DD.json`：

```json
{
  "source": "github_trending",
  "skill": "github-trending",
  "collected_at": "2026-06-10T08:00:00+08:00",
  "items": [
    {
      "name": "owner/repo",
      "url": "https://github.com/owner/repo",
      "summary": "项目名是一个……（做什么），因为……（为什么值得关注）。",
      "stars": 12345,
      "language": "Python",
      "topics": ["ai", "llm", "agent"]
    }
  ]
}
```

字段说明：
- `source`：固定 `github_trending`。
- `skill`：固定 `github-trending`，标记产出来源技能。
- `collected_at`：采集时刻，ISO8601 带时区。
- `items`：Top 15 数组，每条含 `name` / `url` / `summary`（中文）/ `stars` /
  `language` / `topics`；缺失字段填 `null`。

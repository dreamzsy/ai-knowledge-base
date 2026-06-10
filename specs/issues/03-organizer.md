# Issue 03 · organizer 整理任务票

> 来源：specs/agents-prd.md §3.3 / §6
> 状态：todo
> 类型：AFK（输出结构锁定后可自主实现）

## depends_on

- **02-analyzer**：消费其 output（drafted 标注条目）作为本票 input。其 output schema 即本票 input schema。
- 决策前置（HITL，建议先拍）：
  - organizer 的 **MD 输出结构与分组**（§6）—— 本票已给默认结构，见 schema 下方«输出结构»，如不认可需先改。

## What to build

读取 analyzer 产出的 drafted 标注条目，去重后整理成一份 **Markdown 知识文档**，
按主题分组、按价值分排序，写入 `knowledge/articles/<date>.md`。
本票交付「读标注 → 去重 → 渲染 MD → 落盘」这一端到端薄切片。
只整理入库，**不擅自分发、不越过校验发布**（守 §7-5）。

## acceptance

- [ ] 读取指定日期的全部 drafted 条目（无条目时产出空文档骨架或跳过并报告）。
- [ ] 按 `id` 去重：重复条目合并/跳过，不产生重复段落。
- [ ] 按下方«输出结构»渲染 `knowledge/articles/<date>.md`：主题分组 + 组内按 value 降序。
- [ ] 每条至少含标题（链接 source_url）、摘要、3 维标签、价值分。
- [ ] 仅写 `knowledge/articles/`，未触碰 `raw/`、未删除文件、未触发任何分发。
- [ ] 返回本次入库结果的纯 JSON 数组清单（path / count / 去重数）。

## schema

**input**：02-analyzer 的 output schema（drafted 条目）。

**output-1 落盘**：`knowledge/articles/<date>.md`，结构如下：

```markdown
# AI 热门项目日报 · <date>

> 共 N 条 · 来源 GitHub Trending

## 🤖 Agent        <!-- 按 topic 分组，组内按 value 降序 -->

### [<title>](<source_url>)
- **价值** 9/10 · **成熟度** production · **主题** Agent
- <summary>

## 📚 RAG
...
```

**output-2 返回清单**（纯 JSON 数组）：

```json
[
  { "date": "YYYY-MM-DD", "path": "knowledge/articles/<date>.md",
    "count": 0, "deduped": 0, "groups": ["Agent", "RAG"] }
]
```

**输出结构（默认，HITL 可改）**：单文件按日 `articles/<date>.md`；一级分组 = `topic`；
组内按 `value` 降序；条目块 = 标题(链)+ 标签行 + 摘要。

**约束**：`status` 仅推进到 `organized`，**不得置 published**；`channels` 不填、不分发。

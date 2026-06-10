# Issue 02 · analyzer 分析任务票

> 来源：specs/agents-prd.md §3.2 / §6
> 状态：todo
> 类型：AFK（标签维度锁定后可自主实现）

## depends_on

- **01-collector**：消费其 output（raw 条目）作为本票 input。其 output schema 即本票 input schema。
- 决策前置（HITL，建议先拍）：
  - analyzer 的 **3 个标签维度**（§6）—— 本票已给默认定义，见 schema 下方«标签维度»，如不认可需先改。

## What to build

读取 collector 落盘的 raw 条目，对**每条**做内容理解，打 **3 个维度的标签**，
产出 `status=drafted` 的标注条目，以纯 JSON 数组返回交给 organizer。
全程**只读不落盘**：在 raw 事实字段之上附加标注，不改写原始数据。

## acceptance

- [ ] 读取指定日期的全部 raw 条目并逐条处理（无可分析条目时返回 `[]`）。
- [ ] 每条产出 3 个维度标签，取值落在下方«标签维度»的受控枚举内。
- [ ] 事实字段（id/title/source_url/author/时间/metrics）原样沿用 raw，未改写。
- [ ] `id` 沿用 raw，不重新生成；`status` 全部为 `drafted`。
- [ ] 未写入或修改任何文件、未执行任何命令（只读边界）。
- [ ] 返回值为可被 `JSON.parse` 的纯 JSON 数组（无说明文字、无代码围栏）。

## schema

**input**：01-collector 的 output schema（raw 条目）。

**output**（drafted 条目 = 下游 organizer 的 input）：

```json
{
  "id": "YYYY-MM-DD-<slug>",
  "title": "沿用 raw",
  "source_url": "沿用 raw",
  "author": "沿用 raw（无则 null）",
  "published_at": "沿用 raw（无则 null）",
  "collected_at": "沿用 raw",
  "analyzed_at": "ISO8601（本次分析时刻）",
  "summary": "2–4 句中文摘要：是什么 / 解决什么 / 关键点",
  "labels": {
    "topic": "枚举见下",
    "value": 1,
    "maturity": "枚举见下"
  },
  "language": "zh",
  "status": "drafted",
  "raw_ref": "knowledge/raw/<date>/<id>.json"
}
```

**标签维度（3 维，默认定义，HITL 可改）**：
- `topic`（主题，单选枚举）：`LLM` / `Agent` / `RAG` / `Infra` / `App` / `Other`。
- `value`（价值分，1–10 整数）：9-10 改变格局 / 7-8 直接有用 / 5-6 值得了解 / 1-4 可略过；附 `value_reason` 一句话。
- `maturity`（成熟度，单选枚举）：`experimental` / `usable` / `production`。

**约束**：标签取值不得超出枚举；`value` 为 1–10 整数；摘要基于真实内容，不臆造。

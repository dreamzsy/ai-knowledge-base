"""知识库四步自动化流水线.

把「采集 → 分析 → 整理 → 保存」串成一条可命令行驱动的流程，将社区前沿的
AI / LLM / Agent 动态沉淀为符合 ``knowledge/articles/`` 规范的结构化条目。

用途：
    一条命令完成从数据源抓取到知识条目落盘的全过程，支持按数据源与条数裁剪、
    支持干跑（dry-run）预演。
输入：
    - 命令行参数：``--sources`` / ``--limit`` / ``--dry-run`` / ``--verbose``。
    - 环境变量：``LLM_PROVIDER`` 及对应 ``*_API_KEY``（分析步骤需要）。
    - ``pipeline/rss_sources.yaml``：RSS 数据源清单。
输出：
    - ``knowledge/raw/<source>-<date>.json``：采集到的原始数据归档（只追加）。
    - ``knowledge/articles/<id>.json``：分析整理后的知识条目（status=draft）。

四个步骤：
    1. collect  —— GitHub Search API + RSS（httpx 抓取，正则解析 RSS）。
    2. analyze  —— 调用 model_client.chat_with_retry 生成摘要 / 评分 / 标签。
    3. organize —— 去重、字段标准化、规范校验。
    4. save     —— 逐条写入独立 JSON 文件。

依赖：``httpx``（HTTP 抓取）、同目录 ``model_client``（LLM 调用）。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from model_client import chat_with_retry, create_provider, tracker

logger = logging.getLogger("pipeline")

# 路径锚定到仓库根（本文件位于 <repo>/pipeline/pipeline.py）。
REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "knowledge" / "raw"
ARTICLES_DIR = REPO_ROOT / "knowledge" / "articles"
RSS_CONFIG_PATH = Path(__file__).resolve().parent / "rss_sources.yaml"

GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"
GITHUB_QUERY = "AI OR LLM OR agent OR RAG"
HTTP_TIMEOUT = 30.0

# 价值评分阈值：低于此值的条目仍保存为 draft，但不应进入分发（红线 §7.5）。
SCORE_THRESHOLD = 0.6

VALID_STATUSES = ("draft", "reviewed", "published", "archived")

# 可独立调度的流水线步骤（用于 --steps：每日仅 collect，每周 analyze,organize,save）。
VALID_STEPS = ("collect", "analyze", "organize", "save")

# analyze 步骤独立运行时，回看 knowledge/raw/ 多少天内的归档作为输入。
RAW_LOOKBACK_DAYS = 7


@dataclass
class RawItem:
    """采集阶段产出的单条原始记录。

    Attributes:
        source: 数据来源标识（``github_trending`` / ``rss``）。
        title: 条目标题。
        url: 原始链接。
        author: 作者或仓库 owner，可为空。
        summary_raw: 原始描述文本（GitHub description 或 RSS 摘要）。
        published_at: 原始发布时间（ISO 字符串），可为空。
        extra: 来源特有的附加字段（如 stars、language）。
    """

    source: str
    title: str
    url: str
    author: Optional[str] = None
    summary_raw: str = ""
    published_at: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineStats:
    """一次流水线运行的计数统计。

    Attributes:
        collected: 采集到的原始记录数。
        analyzed: 成功完成 LLM 分析的条目数。
        organized: 通过去重与校验的条目数。
        saved: 实际写入磁盘的文件数。
        skipped: 因重复 / 校验失败 / 干跑而未保存的条目数。
    """

    collected: int = 0
    analyzed: int = 0
    organized: int = 0
    saved: int = 0
    skipped: int = 0


# --------------------------------------------------------------------------- #
# Step 1: 采集（Collect）
# --------------------------------------------------------------------------- #
def collect_github(limit: int) -> List[RawItem]:
    """从 GitHub Search API 采集 AI 相关热门仓库。

    按 star 数降序检索匹配 :data:`GITHUB_QUERY` 的仓库。未配置 token 时走匿名
    访问（速率较低），足够小批量采集；命中速率限制会被边界层捕获并记录。

    Args:
        limit: 最多返回的仓库数。

    Returns:
        采集到的 :class:`RawItem` 列表；请求失败时返回空列表。
    """
    params = {
        "q": GITHUB_QUERY,
        "sort": "stars",
        "order": "desc",
        "per_page": str(max(1, min(limit, 100))),
    }
    headers = {"Accept": "application/vnd.github+json"}
    try:
        resp = httpx.get(
            GITHUB_SEARCH_URL, params=params, headers=headers, timeout=HTTP_TIMEOUT
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.error("GitHub 采集失败：%s", exc)
        return []

    items: List[RawItem] = []
    for repo in resp.json().get("items", [])[:limit]:
        items.append(
            RawItem(
                source="github_trending",
                title=repo.get("full_name", repo.get("name", "")),
                url=repo.get("html_url", ""),
                author=(repo.get("owner") or {}).get("login"),
                summary_raw=repo.get("description") or "",
                published_at=repo.get("created_at"),
                extra={
                    "stars": repo.get("stargazers_count", 0),
                    "language": repo.get("language"),
                },
            )
        )
    logger.info("GitHub 采集到 %d 条", len(items))
    return items


def _load_rss_sources() -> List[Dict[str, str]]:
    """读取并解析 RSS 源清单（仅启用项）。

    为避免引入 PyYAML 依赖，用正则解析 ``rss_sources.yaml`` 中的 name/url/enabled
    三个字段（清单结构简单、可控）。

    Returns:
        启用状态为 true 的源字典列表，元素含 ``name`` 与 ``url``。
    """
    if not RSS_CONFIG_PATH.exists():
        logger.warning("未找到 RSS 配置 %s", RSS_CONFIG_PATH)
        return []

    text = RSS_CONFIG_PATH.read_text(encoding="utf-8")
    blocks = re.split(r"\n\s*-\s+name:", text)
    sources: List[Dict[str, str]] = []
    for block in blocks[1:]:
        name = block.splitlines()[0].strip()
        url_match = re.search(r"url:\s*(\S+)", block)
        enabled_match = re.search(r"enabled:\s*(true|false)", block)
        enabled = (enabled_match.group(1) == "true") if enabled_match else True
        if url_match and enabled:
            sources.append({"name": name, "url": url_match.group(1)})
    return sources


def _parse_rss(xml_text: str, source_name: str, limit: int) -> List[RawItem]:
    """用正则从 RSS/Atom 文本提取条目（简易解析，不依赖 XML 库）。

    Args:
        xml_text: RSS/Atom 原始文本。
        source_name: 来源名称，写入 author 字段便于溯源。
        limit: 最多提取的条目数。

    Returns:
        提取出的 :class:`RawItem` 列表。
    """
    items: List[RawItem] = []
    # 同时兼容 RSS <item> 与 Atom <entry>。
    entries = re.findall(r"<(?:item|entry)\b[^>]*>(.*?)</(?:item|entry)>", xml_text, re.S)
    for entry in entries[:limit]:
        title = _extract_tag(entry, "title")
        link = _extract_link(entry)
        desc = _extract_tag(entry, "description") or _extract_tag(entry, "summary")
        pub = _extract_tag(entry, "pubDate") or _extract_tag(entry, "published")
        if not title:
            continue
        items.append(
            RawItem(
                source="rss",
                title=title,
                url=link,
                author=source_name,
                summary_raw=_strip_html(desc),
                published_at=pub or None,
            )
        )
    return items


def collect_rss(limit: int) -> List[RawItem]:
    """从配置的 RSS 源采集条目，按源平摊抓取额度。

    Args:
        limit: 跨所有源的总条目上限。

    Returns:
        采集到的 :class:`RawItem` 列表。
    """
    sources = _load_rss_sources()
    if not sources:
        return []
    per_source = max(1, limit // len(sources))
    items: List[RawItem] = []
    for src in sources:
        if len(items) >= limit:
            break
        try:
            resp = httpx.get(src["url"], timeout=HTTP_TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("RSS 源 %s 抓取失败：%s", src["name"], exc)
            continue
        parsed = _parse_rss(resp.text, src["name"], per_source)
        items.extend(parsed)
        logger.debug("RSS 源 %s 采集 %d 条", src["name"], len(parsed))
    result = items[:limit]
    logger.info("RSS 采集到 %d 条", len(result))
    return result


def _extract_tag(text: str, tag: str) -> str:
    """提取首个 ``<tag>`` 的文本内容，自动剥除 CDATA 包裹。

    Args:
        text: 待搜索的 XML 片段。
        tag: 标签名。

    Returns:
        标签内文本（已 strip）；未命中返回空串。
    """
    match = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", text, re.S)
    if not match:
        return ""
    inner = match.group(1)
    cdata = re.search(r"<!\[CDATA\[(.*?)\]\]>", inner, re.S)
    return (cdata.group(1) if cdata else inner).strip()


def _extract_link(entry: str) -> str:
    """提取条目链接，兼容 RSS ``<link>文本`` 与 Atom ``<link href=...>``。

    Args:
        entry: 单条 item/entry 的 XML 片段。

    Returns:
        链接 URL；未命中返回空串。
    """
    href = re.search(r"<link\b[^>]*href=[\"'](.*?)[\"']", entry, re.S)
    if href:
        return href.group(1).strip()
    return _extract_tag(entry, "link")


def _strip_html(text: str) -> str:
    """去除 HTML 标签并压缩空白，得到纯文本摘要。

    Args:
        text: 可能含 HTML 标签的文本。

    Returns:
        清洗后的纯文本。
    """
    no_tags = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", no_tags).strip()


# --------------------------------------------------------------------------- #
# Step 2: 分析（Analyze）
# --------------------------------------------------------------------------- #
ANALYZE_SYSTEM_PROMPT = (
    "你是 AI 技术知识库的分析助手。针对给定的开源项目或资讯，"
    "用中文产出结构化分析。只返回 JSON，不要任何额外解释或代码块标记。"
)

ANALYZE_USER_TEMPLATE = (
    "请分析以下内容，返回严格的 JSON 对象，字段：\n"
    "  summary: 150 字以内的中文摘要；\n"
    "  highlights: 3-4 条要点字符串数组；\n"
    "  tags: 3-5 个英文技术标签数组（如 LLM、Agent、RAG、Framework）；\n"
    "  score: 0 到 1 之间的价值评分（保留两位小数）；\n"
    "  score_reason: 一句话评分理由。\n\n"
    "标题：{title}\n链接：{url}\n描述：{desc}\n"
)


def analyze_item(provider: Any, raw: RawItem) -> Optional[Dict[str, Any]]:
    """调用 LLM 对单条原始记录生成结构化分析。

    Args:
        provider: 由 ``create_provider`` 构建的 LLM 提供商实例。
        raw: 待分析的 :class:`RawItem`。

    Returns:
        含 summary/highlights/tags/score/score_reason 的字典；
        调用或解析失败时返回 ``None``。
    """
    messages = [
        {"role": "system", "content": ANALYZE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": ANALYZE_USER_TEMPLATE.format(
                title=raw.title, url=raw.url, desc=raw.summary_raw or "（无描述）"
            ),
        },
    ]
    try:
        response = chat_with_retry(provider, messages, temperature=0.3)
    except Exception as exc:  # 边界层：LLM 调用失败不应中断整条流水线
        logger.error("分析失败 [%s]：%s", raw.title, exc)
        return None

    parsed = _parse_json_block(response.content)
    if parsed is None:
        logger.warning("分析结果非合法 JSON [%s]，跳过", raw.title)
    return parsed


def _parse_json_block(text: str) -> Optional[Dict[str, Any]]:
    """从 LLM 文本响应中稳健提取 JSON 对象。

    容忍模型偶尔包裹的 ```json 代码块或前后噪声，截取首个 ``{`` 到末个 ``}``。

    Args:
        text: LLM 返回的原始文本。

    Returns:
        解析出的字典；失败返回 ``None``。
    """
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------- #
# Step 3: 整理（Organize）
# --------------------------------------------------------------------------- #
def _slugify(title: str) -> str:
    """把标题转成适合做文件名的 slug。

    Args:
        title: 原始标题（可能含中文、符号、斜杠）。

    Returns:
        小写、连字符分隔的 ASCII slug；无可用字符时回退为 ``item``。
    """
    base = title.split("/")[-1]  # GitHub full_name 取仓库名部分
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", base).strip("-").lower()
    return slug or "item"


def build_article(raw: RawItem, analysis: Dict[str, Any], today: str) -> Dict[str, Any]:
    """把原始记录与 LLM 分析合并为符合规范的知识条目。

    Args:
        raw: 采集到的原始记录。
        analysis: :func:`analyze_item` 的分析结果。
        today: ``YYYY-MM-DD`` 日期串，用于拼接 id 与 collected_at。

    Returns:
        符合 ``knowledge/articles`` schema 的条目字典（status=draft）。
    """
    score = analysis.get("score")
    try:
        score = round(float(score), 2) if score is not None else None
    except (TypeError, ValueError):
        score = None

    return {
        "id": f"{today}-{_slugify(raw.title)}",
        "title": raw.title,
        "source": raw.source,
        "source_url": raw.url,
        "author": raw.author,
        "published_at": raw.published_at,
        "collected_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "summary": analysis.get("summary", ""),
        "highlights": analysis.get("highlights") or [],
        "tags": analysis.get("tags") or [],
        "language": "zh",
        "score": score,
        "score_reason": analysis.get("score_reason", ""),
        "status": "draft",
        "channels": [],
        "extra": raw.extra,
    }


def organize(articles: List[Dict[str, Any]], stats: PipelineStats) -> List[Dict[str, Any]]:
    """去重并校验条目，剔除重复 id 与不合规记录。

    去重以 ``id`` 为键（保留首次出现）。校验要求 title/summary 非空且 status 合法；
    缺少 score 或 score 偏低不阻断保存，仅告警（分发由 curator 把关，红线 §7.5）。

    Args:
        articles: 待整理的条目列表。
        stats: 运行统计对象，原地累加 organized/skipped。

    Returns:
        通过去重与校验的条目列表。
    """
    seen: set = set()
    result: List[Dict[str, Any]] = []
    for art in articles:
        art_id = art.get("id", "")
        if not art_id or art_id in seen:
            logger.debug("跳过重复/缺失 id：%r", art_id)
            stats.skipped += 1
            continue
        if not _validate_article(art):
            stats.skipped += 1
            continue
        seen.add(art_id)
        if art.get("score") is not None and art["score"] < SCORE_THRESHOLD:
            logger.info("条目 %s 评分 %.2f 低于阈值，保留为 draft 不进入分发",
                        art_id, art["score"])
        result.append(art)
    stats.organized = len(result)
    return result


def _validate_article(art: Dict[str, Any]) -> bool:
    """校验单条条目是否满足保存的最低要求。

    Args:
        art: 待校验条目。

    Returns:
        合规返回 ``True``；否则记录告警并返回 ``False``。
    """
    if not art.get("title") or not art.get("summary"):
        logger.warning("条目 %s 缺少 title/summary，跳过", art.get("id"))
        return False
    if art.get("status") not in VALID_STATUSES:
        logger.warning("条目 %s status 非法：%r", art.get("id"), art.get("status"))
        return False
    return True


# --------------------------------------------------------------------------- #
# Step 4: 保存（Save）
# --------------------------------------------------------------------------- #
def save_articles(
    articles: List[Dict[str, Any]], stats: PipelineStats, dry_run: bool
) -> None:
    """把条目逐条写入 ``knowledge/articles/<id>.json``。

    已存在同名文件时跳过（不覆盖），避免误改已有条目（呼应红线 §7.2）。
    dry-run 模式只记录将要写入的路径，不落盘。

    Args:
        articles: 待保存的条目列表。
        stats: 运行统计对象，原地累加 saved/skipped。
        dry_run: 为 ``True`` 时不实际写文件。
    """
    if not dry_run:
        ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
    for art in articles:
        path = ARTICLES_DIR / f"{art['id']}.json"
        if path.exists():
            logger.info("已存在，跳过不覆盖：%s", path.name)
            stats.skipped += 1
            continue
        if dry_run:
            logger.info("[dry-run] 将写入 %s", path.name)
            continue
        path.write_text(
            json.dumps(art, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        logger.info("已保存 %s", path.name)
        stats.saved += 1


def archive_raw(items: List[RawItem], source_label: str, today: str, dry_run: bool) -> None:
    """把本次采集的原始记录追加归档到 ``knowledge/raw/``。

    Args:
        items: 采集到的原始记录。
        source_label: 来源标签，用于文件名。
        today: ``YYYY-MM-DD`` 日期串。
        dry_run: 为 ``True`` 时不落盘。
    """
    if not items or dry_run:
        return
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_DIR / f"{source_label}-{today}.json"
    payload = [vars(it) for it in items]
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    logger.info("原始数据归档至 %s（%d 条）", path.name, len(items))


def load_recent_raw(lookback_days: int = RAW_LOOKBACK_DAYS) -> List[RawItem]:
    """从 ``knowledge/raw/`` 回读近 N 天归档的原始记录。

    供 analyze 步骤独立运行时使用：每日采集与每周分析相隔数天，分析阶段
    需把这段时间内 collect 落盘的原始数据重新装载回内存。按文件名末尾的
    ``YYYY-MM-DD`` 过滤日期窗口（解析失败的文件一律纳入，从宽不漏数据）。

    Args:
        lookback_days: 回看天数（含今天），早于该窗口的归档忽略。

    Returns:
        窗口内全部原始记录；目录不存在或无匹配时返回空列表。
    """
    if not RAW_DIR.exists():
        logger.warning("原始归档目录不存在：%s", RAW_DIR)
        return []
    cutoff = datetime.now().date() - timedelta(days=lookback_days - 1)
    items: List[RawItem] = []
    for path in sorted(RAW_DIR.glob("*.json")):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", path.stem)
        if m:
            try:
                if datetime.strptime(m.group(1), "%Y-%m-%d").date() < cutoff:
                    continue
            except ValueError:
                pass  # 文件名日期异常时不丢弃，交由后续按内容处理。
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("跳过无法读取的归档 %s：%s", path.name, exc)
            continue
        for rec in payload:
            known = {f for f in RawItem.__dataclass_fields__}
            items.append(RawItem(**{k: v for k, v in rec.items() if k in known}))
    logger.info("回读 %d 天内原始记录共 %d 条", lookback_days, len(items))
    return items


# --------------------------------------------------------------------------- #
# 编排（Orchestration）
# --------------------------------------------------------------------------- #
def run_pipeline(
    sources: List[str],
    limit: int,
    dry_run: bool,
    steps: Optional[List[str]] = None,
) -> PipelineStats:
    """按 采集 → 分析 → 整理 → 保存 顺序执行流水线，支持只跑其中部分步骤。

    步骤可拆开调度（每日仅 ``collect``，每周 ``analyze,organize,save``）。当
    ``analyze`` 在没有 ``collect`` 的情况下运行时，自动从 ``knowledge/raw/``
    回读近 :data:`RAW_LOOKBACK_DAYS` 天的归档作为输入。

    Args:
        sources: 启用的数据源列表（``github`` / ``rss`` 的子集）。
        limit: 每个数据源的采集条数上限。
        dry_run: 干跑模式，不写任何文件，且跳过需要 API Key 的真实分析。
        steps: 要执行的步骤子集；``None`` 表示全部四步。

    Returns:
        本次运行的 :class:`PipelineStats`。
    """
    active = list(steps) if steps else list(VALID_STEPS)
    stats = PipelineStats()
    today = datetime.now().strftime("%Y-%m-%d")

    # Step 1: 采集
    raw_items: List[RawItem] = []
    if "collect" in active:
        if "github" in sources:
            gh = collect_github(limit)
            archive_raw(gh, "github", today, dry_run)
            raw_items.extend(gh)
        if "rss" in sources:
            rss = collect_rss(limit)
            archive_raw(rss, "rss", today, dry_run)
            raw_items.extend(rss)
        stats.collected = len(raw_items)
        logger.info("Step 1 采集完成：共 %d 条", stats.collected)
    elif "analyze" in active:
        # 分析独立调度：采集发生在数天前，从归档回读原始数据。
        raw_items = load_recent_raw()

    # Step 2: 分析（干跑不消耗 API 额度）
    articles: List[Dict[str, Any]] = []
    if "analyze" in active:
        if not raw_items:
            logger.warning("无原始数据可分析，流水线结束")
            return stats
        if dry_run:
            logger.info("[dry-run] 跳过 LLM 分析，仅预演 %d 条", len(raw_items))
            for raw in raw_items:
                stub = {"summary": raw.summary_raw or raw.title, "highlights": [],
                        "tags": [], "score": None, "score_reason": "dry-run"}
                articles.append(build_article(raw, stub, today))
        else:
            provider = create_provider()
            for raw in raw_items:
                analysis = analyze_item(provider, raw)
                if analysis is None:
                    stats.skipped += 1
                    continue
                stats.analyzed += 1
                articles.append(build_article(raw, analysis, today))
        logger.info("Step 2 分析完成：成功 %d 条",
                    stats.analyzed if not dry_run else len(articles))
    elif "collect" in active:
        # 只采集不分析：原始数据已归档，本次到此为止。
        logger.info("仅执行采集，原始数据已归档，跳过后续步骤")
        return stats

    # Step 3: 整理
    organized = articles
    if "organize" in active:
        organized = organize(articles, stats)
        logger.info("Step 3 整理完成：保留 %d 条", stats.organized)

    # Step 4: 保存
    if "save" in active:
        save_articles(organized, stats, dry_run)
        logger.info("Step 4 保存完成：写入 %d 条", stats.saved)

    # 运行结束：输出本次 LLM 成本估算（按当前 provider；纯采集时为空报告）。
    tracker.report(provider=os.getenv("LLM_PROVIDER", "deepseek").lower())
    return stats


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """解析命令行参数。

    Args:
        argv: 参数列表；为 ``None`` 时取 ``sys.argv``。

    Returns:
        解析后的 :class:`argparse.Namespace`。
    """
    parser = argparse.ArgumentParser(
        description="知识库四步自动化流水线：采集 → 分析 → 整理 → 保存。"
    )
    parser.add_argument(
        "--sources",
        default="github,rss",
        help="逗号分隔的数据源，可选 github / rss（默认 github,rss）。",
    )
    parser.add_argument(
        "--limit", type=int, default=20, help="每个数据源的采集条数上限（默认 20）。"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="干跑：不调用 LLM、不写文件。"
    )
    parser.add_argument(
        "--verbose", action="store_true", help="输出 DEBUG 级详细日志。"
    )
    parser.add_argument(
        "--steps",
        default="collect,analyze,organize,save",
        help=(
            "逗号分隔的执行步骤，可选 collect / analyze / organize / save"
            "（默认全部）。每日仅采集用 --steps collect；"
            "每周分析用 --steps analyze,organize,save。"
        ),
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    """CLI 入口。

    Args:
        argv: 参数列表；为 ``None`` 时取 ``sys.argv``。

    Returns:
        进程退出码（0 成功，2 参数错误）。
    """
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    sources = [s.strip().lower() for s in args.sources.split(",") if s.strip()]
    invalid = [s for s in sources if s not in ("github", "rss")]
    if invalid or not sources:
        logger.error("无效的 --sources：%s（可选 github / rss）", invalid or "空")
        return 2

    steps = [s.strip().lower() for s in args.steps.split(",") if s.strip()]
    bad_steps = [s for s in steps if s not in VALID_STEPS]
    if bad_steps or not steps:
        logger.error("无效的 --steps：%s（可选 %s）",
                     bad_steps or "空", " / ".join(VALID_STEPS))
        return 2

    logger.info(
        "启动流水线 sources=%s limit=%d dry_run=%s steps=%s",
        sources, args.limit, args.dry_run, steps,
    )
    stats = run_pipeline(sources, args.limit, args.dry_run, steps)
    logger.info(
        "运行汇总：采集 %d / 分析 %d / 整理 %d / 保存 %d / 跳过 %d",
        stats.collected, stats.analyzed, stats.organized, stats.saved, stats.skipped,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

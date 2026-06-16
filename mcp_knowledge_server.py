"""本地知识库 MCP Server.

通过 JSON-RPC 2.0 over stdio 暴露知识库检索能力，供 MCP 客户端调用。

用途：
    让 AI 工具搜索 ``knowledge/articles/`` 下的结构化知识条目。
输入：
    stdin 上逐行传入的 JSON-RPC 2.0 请求（initialize / tools/list / tools/call）。
输出：
    stdout 上逐行返回的 JSON-RPC 2.0 响应。

提供的 MCP 工具：
    - search_articles(keyword, limit=5): 按关键词搜索标题与摘要。
    - get_article(article_id): 按 ID 获取完整条目。
    - knowledge_stats(): 返回文章总数、来源分布与热门标签。

仅依赖 Python 标准库，无第三方依赖。
"""

from __future__ import annotations

import json
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Any, TypedDict

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "knowledge-base", "version": "0.1.0"}
ARTICLES_DIR = Path(__file__).resolve().parent / "knowledge" / "articles"


class Article(TypedDict, total=False):
    """知识条目结构（字段与 knowledge/articles/<id>.json 对齐）。"""

    id: str
    title: str
    source: str
    source_url: str
    summary: str
    highlights: list[str]
    tags: list[str]
    score: float
    status: str


def load_articles() -> list[Article]:
    """加载 ``knowledge/articles/`` 下的全部知识条目。

    Returns:
        解析成功的条目列表；目录不存在时返回空列表。
    """
    if not ARTICLES_DIR.is_dir():
        logger.warning("articles dir not found: %s", ARTICLES_DIR)
        return []

    articles: list[Article] = []
    for path in sorted(ARTICLES_DIR.glob("*.json")):
        try:
            with path.open(encoding="utf-8") as fp:
                articles.append(json.load(fp))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("skip invalid article %s: %s", path.name, exc)
    return articles


def search_articles(keyword: str, limit: int = 5) -> list[dict[str, Any]]:
    """按关键词搜索文章标题与摘要。

    Args:
        keyword: 检索词，大小写不敏感。
        limit: 返回条数上限，默认 5。

    Returns:
        命中条目的精简列表，按 score 降序排列，每项含 id/title/source/score/summary。
    """
    needle = keyword.strip().lower()
    hits: list[Article] = []
    for art in load_articles():
        haystack = f"{art.get('title', '')}\n{art.get('summary', '')}".lower()
        if not needle or needle in haystack:
            hits.append(art)

    hits.sort(key=lambda a: a.get("score", 0) or 0, reverse=True)
    return [
        {
            "id": art.get("id"),
            "title": art.get("title"),
            "source": art.get("source"),
            "score": art.get("score"),
            "summary": art.get("summary"),
        }
        for art in hits[: max(0, limit)]
    ]


def get_article(article_id: str) -> dict[str, Any] | None:
    """按 ID 获取文章完整内容。

    Args:
        article_id: 知识条目唯一 ID。

    Returns:
        匹配的完整条目；未找到时返回 None。
    """
    for art in load_articles():
        if art.get("id") == article_id:
            return dict(art)
    return None


def knowledge_stats() -> dict[str, Any]:
    """统计知识库概况。

    Returns:
        含文章总数(total)、来源分布(by_source)、热门标签(top_tags)的字典。
    """
    articles = load_articles()
    sources: Counter[str] = Counter()
    tags: Counter[str] = Counter()
    for art in articles:
        sources[art.get("source", "unknown")] += 1
        for tag in art.get("tags", []):
            tags[tag] += 1

    return {
        "total": len(articles),
        "by_source": dict(sources.most_common()),
        "top_tags": dict(tags.most_common(10)),
    }


TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_articles",
        "description": "按关键词搜索文章标题和摘要，返回最相关的若干条。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "检索关键词"},
                "limit": {"type": "integer", "description": "返回条数上限", "default": 5},
            },
            "required": ["keyword"],
        },
    },
    {
        "name": "get_article",
        "description": "按 ID 获取文章完整内容。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "article_id": {"type": "string", "description": "知识条目唯一 ID"},
            },
            "required": ["article_id"],
        },
    },
    {
        "name": "knowledge_stats",
        "description": "返回知识库统计信息：文章总数、来源分布、热门标签。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def dispatch_tool(name: str, arguments: dict[str, Any]) -> Any:
    """根据工具名调用对应实现。

    Args:
        name: 工具名称。
        arguments: 工具入参。

    Returns:
        工具返回的可 JSON 序列化结果。

    Raises:
        ValueError: 工具名未知或必填参数缺失。
    """
    match name:
        case "search_articles":
            if "keyword" not in arguments:
                raise ValueError("missing required argument: keyword")
            return search_articles(arguments["keyword"], arguments.get("limit", 5))
        case "get_article":
            if "article_id" not in arguments:
                raise ValueError("missing required argument: article_id")
            return get_article(arguments["article_id"])
        case "knowledge_stats":
            return knowledge_stats()
        case _:
            raise ValueError(f"unknown tool: {name}")


def handle_request(req: dict[str, Any]) -> dict[str, Any] | None:
    """处理单条 JSON-RPC 2.0 请求。

    Args:
        req: 解析后的请求对象。

    Returns:
        JSON-RPC 响应对象；通知（无 id）返回 None 表示不应答。
    """
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params") or {}

    # 通知（如 notifications/initialized）无需应答。
    if req_id is None and method != "initialize":
        return None

    try:
        match method:
            case "initialize":
                result: Any = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": SERVER_INFO,
                }
            case "tools/list":
                result = {"tools": TOOLS}
            case "tools/call":
                payload = dispatch_tool(params.get("name", ""), params.get("arguments") or {})
                text = json.dumps(payload, ensure_ascii=False, indent=2)
                result = {"content": [{"type": "text", "text": text}]}
            case _:
                return _error(req_id, -32601, f"method not found: {method}")
    except ValueError as exc:
        return _error(req_id, -32602, str(exc))
    except Exception as exc:  # noqa: BLE001 — stdio 边界统一兜底，避免进程崩溃。
        logger.exception("internal error handling %s", method)
        return _error(req_id, -32603, f"internal error: {exc}")

    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    """构造 JSON-RPC 错误响应。"""
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def main() -> None:
    """从 stdin 逐行读取请求，向 stdout 逐行写回响应。"""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            sys.stdout.write(json.dumps(_error(None, -32700, f"parse error: {exc}")) + "\n")
            sys.stdout.flush()
            continue

        resp = handle_request(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()

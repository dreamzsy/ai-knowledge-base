"""知识条目五维质量评分工具。

对知识条目 JSON 从五个维度加权评分（满分 100）：
摘要质量、技术深度、格式规范、标签精度、空洞词检测；
输出可视化进度条、各维度得分与 A/B/C 等级。

命令行用法：
    python hooks/check_quality.py <json_file> [json_file2 ...]

存在 C 级条目时退出码为 1，否则为 0。
"""

from __future__ import annotations

import glob
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# 各维度满分（加权总分 100）。
MAX_SUMMARY = 25
MAX_DEPTH = 25
MAX_FORMAT = 20
MAX_TAGS = 15
MAX_BUZZWORD = 15
MAX_TOTAL = MAX_SUMMARY + MAX_DEPTH + MAX_FORMAT + MAX_TAGS + MAX_BUZZWORD

GRADE_A = 80
GRADE_B = 60

# 摘要技术关键词（命中给奖励分）。
TECH_KEYWORDS: tuple[str, ...] = (
    "LLM", "Agent", "RAG", "MCP", "Transformer", "模型", "框架", "推理",
    "训练", "微调", "向量", "检索", "多模态", "API", "开源", "部署",
)

# 标准标签词表（用于标签精度校验）。
STANDARD_TAGS: frozenset[str] = frozenset({
    "LLM", "Agent", "Multi-Agent", "RAG", "MCP", "Framework", "CLI",
    "Memory", "Workflow", "Finance", "Infra", "App", "Local-Deployment",
})

# 空洞词黑名单（中 / 英两组）。
BUZZWORDS_ZH: tuple[str, ...] = (
    "赋能", "抓手", "闭环", "打通", "全链路", "底层逻辑",
    "颗粒度", "对齐", "拉通", "沉淀", "强大的", "革命性的",
)
BUZZWORDS_EN: tuple[str, ...] = (
    "groundbreaking", "revolutionary", "game-changing", "cutting-edge",
    "next-generation", "state-of-the-art", "best-in-class",
)


@dataclass
class DimensionScore:
    """单个评分维度的结果。

    Attributes:
        name: 维度名称。
        score: 实际得分。
        max_score: 该维度满分。
        notes: 评分说明（命中/扣分原因）。
    """

    name: str
    score: float
    max_score: int
    notes: list[str] = field(default_factory=list)


@dataclass
class QualityReport:
    """单个文件的完整质量报告。

    Attributes:
        path: 文件路径。
        dimensions: 五个维度得分。
        total: 加权总分。
        grade: 等级 A / B / C。
        error: 文件级错误（如解析失败），非空时其余字段无意义。
    """

    path: str
    dimensions: list[DimensionScore] = field(default_factory=list)
    total: float = 0.0
    grade: str = ""
    error: str = ""


def _normalize_score(raw: object) -> float | None:
    """将 score 归一化到 0-10。兼容 0-1 制与 1-10 制。

    Args:
        raw: 原始 score 字段值。

    Returns:
        归一化到 0-10 的分值；无法解析时返回 None。
    """
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return None
    # 约定：<=1 视为 0-1 制，放大到 0-10；否则视为 1-10 制。
    return float(raw) * 10 if raw <= 1 else float(raw)


def score_summary(entry: dict[str, object]) -> DimensionScore:
    """摘要质量（25 分）：长度 + 技术关键词奖励。"""
    dim = DimensionScore("摘要质量", 0.0, MAX_SUMMARY)
    summary = entry.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        dim.notes.append("缺少摘要")
        return dim
    text = summary.strip()
    length = len(text)
    if length >= 50:
        base = 20.0
        dim.notes.append(f"长度 {length} 字(满)")
    elif length >= 20:
        base = 12.0
        dim.notes.append(f"长度 {length} 字(基本分)")
    else:
        base = 5.0
        dim.notes.append(f"长度 {length} 字(过短)")
    hits = [k for k in TECH_KEYWORDS if k.lower() in text.lower()]
    bonus = min(5.0, len(hits) * 2.5)
    if hits:
        dim.notes.append(f"技术关键词 +{bonus:.0f}: {hits[:3]}")
    dim.score = min(MAX_SUMMARY, base + bonus)
    return dim


def score_depth(entry: dict[str, object]) -> DimensionScore:
    """技术深度（25 分）：基于 score 字段，0-10 映射到 0-25。"""
    dim = DimensionScore("技术深度", 0.0, MAX_DEPTH)
    norm = _normalize_score(entry.get("score"))
    if norm is None:
        dim.notes.append("无 score 字段或非数值，按 0 计")
        return dim
    dim.score = round(norm / 10 * MAX_DEPTH, 1)
    dim.notes.append(f"score={entry.get('score')} → 归一 {norm:.1f}/10")
    return dim


def score_format(entry: dict[str, object]) -> DimensionScore:
    """格式规范（20 分）：五项各 4 分。"""
    dim = DimensionScore("格式规范", 0.0, MAX_FORMAT)
    checks = {
        "id": entry.get("id"),
        "title": entry.get("title"),
        "source_url": entry.get("source_url"),
        "status": entry.get("status"),
        "时间戳": entry.get("collected_at") or entry.get("published_at"),
    }
    for name, value in checks.items():
        if isinstance(value, str) and value.strip():
            dim.score += 4
        else:
            dim.notes.append(f"缺 {name}")
    return dim


def score_tags(entry: dict[str, object]) -> DimensionScore:
    """标签精度（15 分）：1-3 个合法标签最佳。"""
    dim = DimensionScore("标签精度", 0.0, MAX_TAGS)
    tags = entry.get("tags")
    if not isinstance(tags, list) or not tags:
        dim.notes.append("无标签")
        return dim
    n = len(tags)
    valid = [t for t in tags if t in STANDARD_TAGS]
    if 1 <= n <= 3:
        count_score = 9.0
        dim.notes.append(f"数量 {n}(最佳)")
    else:
        count_score = 5.0
        dim.notes.append(f"数量 {n}(偏多/偏少)")
    ratio = len(valid) / n
    valid_score = round(ratio * 6, 1)
    dim.notes.append(f"合法标签 {len(valid)}/{n}")
    dim.score = min(MAX_TAGS, count_score + valid_score)
    return dim


def score_buzzword(entry: dict[str, object]) -> DimensionScore:
    """空洞词检测（15 分）：每命中一个扣 5 分，扣完为止。"""
    dim = DimensionScore("空洞词检测", float(MAX_BUZZWORD), MAX_BUZZWORD)
    parts = [entry.get("summary"), entry.get("title")]
    text = " ".join(p for p in parts if isinstance(p, str)).lower()
    hits = [w for w in BUZZWORDS_ZH if w in text]
    hits += [w for w in BUZZWORDS_EN if w in text]
    if hits:
        dim.score = max(0.0, MAX_BUZZWORD - len(hits) * 5)
        dim.notes.append(f"命中空洞词 {hits}")
    else:
        dim.notes.append("无空洞词")
    return dim


def build_report(path: Path) -> QualityReport:
    """读取并评分单个文件，返回质量报告。"""
    report = QualityReport(path=str(path))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        report.error = f"无法读取: {exc}"
        return report
    except json.JSONDecodeError as exc:
        report.error = f"JSON 解析失败: {exc}"
        return report
    if not isinstance(data, dict):
        report.error = "顶层结构应为对象(dict)"
        return report

    report.dimensions = [
        score_summary(data), score_depth(data), score_format(data),
        score_tags(data), score_buzzword(data),
    ]
    report.total = round(sum(d.score for d in report.dimensions), 1)
    if report.total >= GRADE_A:
        report.grade = "A"
    elif report.total >= GRADE_B:
        report.grade = "B"
    else:
        report.grade = "C"
    return report


def _bar(score: float, max_score: int, width: int = 20) -> str:
    """生成可视化进度条。"""
    filled = int(round(score / max_score * width)) if max_score else 0
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def render(report: QualityReport) -> None:
    """打印单个报告（进度条 + 各维度 + 等级）。"""
    if report.error:
        logger.error("✗ %s\n    %s", report.path, report.error)
        return
    logger.info("%s  [等级 %s · %.1f/%d]", report.path, report.grade,
                report.total, MAX_TOTAL)
    for d in report.dimensions:
        logger.info("  %-6s %s %5.1f/%-2d  %s", d.name,
                    _bar(d.score, d.max_score), d.score, d.max_score,
                    "; ".join(d.notes))


def _expand(argv: list[str]) -> list[Path]:
    """展开命令行参数中的通配符与普通路径。"""
    paths: list[Path] = []
    for arg in argv:
        if any(c in arg for c in "*?["):
            paths.extend(sorted(Path(p) for p in glob.glob(arg)))
        else:
            paths.append(Path(arg))
    return paths


def main(argv: list[str]) -> int:
    """命令行入口。存在 C 级返回 1，否则 0。"""
    if not argv:
        logger.error("用法: python hooks/check_quality.py <json_file> ...")
        return 1
    paths = _expand(argv)
    grades = {"A": 0, "B": 0, "C": 0}
    errors = 0
    for path in paths:
        report = build_report(path)
        render(report)
        if report.error:
            errors += 1
        else:
            grades[report.grade] += 1
    logger.info("-" * 56)
    logger.info("汇总: A=%d  B=%d  C=%d  错误=%d  (共 %d)",
                grades["A"], grades["B"], grades["C"], errors, len(paths))
    return 1 if grades["C"] > 0 or errors > 0 else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

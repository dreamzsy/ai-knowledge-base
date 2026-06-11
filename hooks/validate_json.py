"""知识条目 JSON 校验工具。

校验 ``knowledge/articles/`` 下的知识条目 JSON 文件是否符合规范：
字段存在性与类型、ID 格式、status 枚举、URL 格式、摘要长度、标签数量，
以及可选字段 ``score`` / ``audience`` 的取值范围。

命令行用法：
    python hooks/validate_json.py <json_file> [json_file2 ...]

校验全部通过时退出码为 0；存在任何失败时打印错误列表与汇总统计并退出码为 1。
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# 必填字段 -> 期望类型。同时用于校验字段存在性与类型。
REQUIRED_FIELDS: dict[str, type] = {
    "id": str,
    "title": str,
    "source_url": str,
    "summary": str,
    "tags": list,
    "status": str,
}

VALID_STATUSES: frozenset[str] = frozenset(
    {"draft", "review", "published", "archived"}
)
VALID_AUDIENCES: frozenset[str] = frozenset(
    {"beginner", "intermediate", "advanced"}
)

# ID 形如 {source}-{YYYYMMDD}-{NNN}，例如 github-20260317-001。
ID_PATTERN: re.Pattern[str] = re.compile(r"^[a-z0-9_]+-\d{8}-\d{3}$")
URL_PATTERN: re.Pattern[str] = re.compile(r"^https?://\S+$", re.IGNORECASE)

MIN_SUMMARY_LEN: int = 20
MIN_TAGS: int = 1
SCORE_MIN: int = 1
SCORE_MAX: int = 10


def validate_entry(data: dict[str, object]) -> list[str]:
    """校验单条知识条目，返回错误信息列表（空列表表示通过）。

    Args:
        data: 已解析的 JSON 对象。

    Returns:
        错误描述字符串列表；为空表示该条目校验通过。
    """
    errors: list[str] = []

    # 1. 必填字段：存在性 + 类型。
    for field, expected_type in REQUIRED_FIELDS.items():
        if field not in data:
            errors.append(f"缺少必填字段: {field}")
            continue
        value = data[field]
        # 注意 bool 是 int 的子类，此处字段无 int/bool，无需特判。
        if not isinstance(value, expected_type):
            errors.append(
                f"字段 {field} 类型错误: 期望 {expected_type.__name__}, "
                f"实际 {type(value).__name__}"
            )

    # 后续检查依赖字段已存在且类型正确，逐项防御性取值。
    entry_id = data.get("id")
    if isinstance(entry_id, str) and not ID_PATTERN.match(entry_id):
        errors.append(
            f"id 格式错误: {entry_id!r} 不符合 {{source}}-{{YYYYMMDD}}-{{NNN}}"
        )

    status = data.get("status")
    if isinstance(status, str) and status not in VALID_STATUSES:
        errors.append(
            f"status 非法: {status!r}, 应为 {sorted(VALID_STATUSES)} 之一"
        )

    source_url = data.get("source_url")
    if isinstance(source_url, str) and not URL_PATTERN.match(source_url):
        errors.append(f"source_url 格式错误: {source_url!r} 应为 http(s)://...")

    summary = data.get("summary")
    if isinstance(summary, str) and len(summary.strip()) < MIN_SUMMARY_LEN:
        errors.append(
            f"summary 过短: 至少 {MIN_SUMMARY_LEN} 字, 实际 {len(summary.strip())}"
        )

    tags = data.get("tags")
    if isinstance(tags, list) and len(tags) < MIN_TAGS:
        errors.append(f"tags 至少需要 {MIN_TAGS} 个")

    # 2. 可选字段：存在时才校验。
    if "score" in data:
        score = data["score"]
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            errors.append(f"score 类型错误: 期望数值, 实际 {type(score).__name__}")
        elif not SCORE_MIN <= score <= SCORE_MAX:
            errors.append(f"score 超出范围 [{SCORE_MIN}, {SCORE_MAX}]: {score}")

    if "audience" in data:
        audience = data["audience"]
        if audience not in VALID_AUDIENCES:
            errors.append(
                f"audience 非法: {audience!r}, 应为 {sorted(VALID_AUDIENCES)} 之一"
            )

    return errors


def validate_file(path: Path) -> list[str]:
    """校验单个 JSON 文件，返回该文件的错误信息列表。

    Args:
        path: JSON 文件路径。

    Returns:
        错误描述字符串列表；为空表示该文件校验通过。
    """
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"无法读取文件: {exc}"]

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return [f"JSON 解析失败: {exc}"]

    if not isinstance(data, dict):
        return [f"顶层结构应为对象(dict), 实际为 {type(data).__name__}"]

    return validate_entry(data)


def main(argv: list[str]) -> int:
    """命令行入口。

    Args:
        argv: 不含程序名的命令行参数（一个或多个文件路径，支持通配符）。

    Returns:
        进程退出码：全部通过为 0，存在失败为 1。
    """
    if not argv:
        logger.error("用法: python hooks/validate_json.py <json_file> [json_file2 ...]")
        return 1

    # 支持 shell 未展开的通配符：仅对含通配符的参数做展开，
    # 并正确处理绝对路径（Path().glob 不接受绝对模式）。
    paths: list[Path] = []
    for arg in argv:
        if any(ch in arg for ch in "*?[") :
            arg_path = Path(arg)
            if arg_path.is_absolute():
                matches = sorted(Path(arg_path.anchor).glob(
                    str(arg_path.relative_to(arg_path.anchor))
                ))
            else:
                matches = sorted(Path().glob(arg))
            if matches:
                paths.extend(matches)
            else:
                logger.error("✗ %s", arg)
                logger.error("    - 通配符未匹配到任何文件")
                paths.append(None)  # 占位，计入失败统计
        else:
            paths.append(Path(arg))

    total = 0
    passed = 0
    failed = 0
    for path in paths:
        total += 1
        if path is None:  # 通配符未匹配，已打印错误
            failed += 1
            continue
        errors = validate_file(path)
        if errors:
            failed += 1
            logger.error("✗ %s", path)
            for err in errors:
                logger.error("    - %s", err)
        else:
            passed += 1
            logger.info("✓ %s", path)

    logger.info("-" * 48)
    logger.info("汇总: 共 %d 个文件, 通过 %d, 失败 %d", total, passed, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""Claude Code PostToolUse hook —— 知识条目写入后自动校验。

Claude Code 在 Write/Edit 工具执行后,通过 stdin 灌入一段 JSON 负载,
本脚本从中取出被写文件路径,若位于 ``knowledge/articles/`` 下且为 JSON,
则依次调用格式校验与质量评分脚本:

    - hooks/validate_json.py: 硬门禁,失败时以退出码 2 把错误回喂给 Claude;
    - hooks/check_quality.py: 仅作质量提示,不阻断写入。

非知识条目路径直接放行(退出码 0),避免编辑脚本自身时被反复触发。

退出码语义(Claude Code 约定):
    0  放行(stdout 在 transcript 模式可见);
    2  阻断并把 stderr 回喂给 Claude 让其修正;
    其它  错误展示给用户,但不回喂。
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# 仅校验该目录下的知识条目。
TARGET_DIR = "knowledge/articles"
# 触发校验的工具名。
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit"})
# 校验脚本路径(相对仓库根)。
VALIDATOR = Path("hooks/validate_json.py")
QUALITY = Path("hooks/check_quality.py")


def _extract_path(payload: dict[str, object]) -> str | None:
    """从 hook 负载中取出被写文件路径。"""
    tool = payload.get("tool_name")
    if tool not in WRITE_TOOLS:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    file_path = tool_input.get("file_path")
    return file_path if isinstance(file_path, str) else None


def _is_article(file_path: str) -> bool:
    """判断是否为需要校验的知识条目路径。"""
    return TARGET_DIR in file_path.replace("\\", "/") and file_path.endswith(".json")


def _run(script: Path, file_path: str) -> subprocess.CompletedProcess[str]:
    """以子进程方式运行校验脚本,捕获输出,绝不抛异常。"""
    return subprocess.run(
        [sys.executable, str(script), file_path],
        capture_output=True,
        text=True,
        check=False,
    )


def main() -> int:
    """读取 stdin 负载,执行校验,返回 Claude Code 约定退出码。"""
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        # 负载无法解析时放行,不阻塞工作流。
        return 0
    if not isinstance(payload, dict):
        return 0

    file_path = _extract_path(payload)
    if not file_path or not _is_article(file_path):
        return 0

    # 1) 格式校验:硬门禁。
    fmt = _run(VALIDATOR, file_path)
    if fmt.returncode != 0:
        sys.stderr.write(
            f"知识条目格式校验未通过,请按以下提示修正 {file_path}:\n"
            f"{fmt.stdout}{fmt.stderr}"
        )
        return 2

    # 2) 质量评分:仅提示,不阻断。check_quality 经 logging 输出到 stderr。
    qa = _run(QUALITY, file_path)
    report = (qa.stdout + qa.stderr).rstrip()
    logger.info("[hook] ✓ 格式通过 · 质量评分:\n%s", report)
    return 0


if __name__ == "__main__":
    sys.exit(main())

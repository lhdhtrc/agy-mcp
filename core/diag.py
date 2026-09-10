"""本地探测：工作树 diff 与代理环境，用来排障和给轮次补齐上下文。

这里不碰网络，也不起进程（`git` 除外）：`collect_diff` 是在本地读出改动再作为文本喂给
agent——让 agent 自己跑 `git diff` 在沙箱下并不可靠（实测几分钟都出不来）。
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

from core.agy import _git


def collect_diff(cwd: str, base: Optional[str], limit: int) -> Tuple[str, str]:
    """在本地抓取工作树 diff，返回 (diff 文本, 说明)。说明非空表示没接上或走了兜底。"""
    code, out = _git(["rev-parse", "--is-inside-work-tree"], cwd)
    if code != 0 or "true" not in out.lower():
        return "", "not a git working tree, so no diff was attached"

    target = base or "HEAD"
    code, out = _git(["diff", target], cwd)
    if code != 0:
        # 还没有提交（或 ref 不存在）：退回到"未暂存 + 已暂存"。
        _, unstaged = _git(["diff"], cwd)
        _, staged = _git(["diff", "--cached"], cwd)
        out = unstaged + staged
        target = "the index"

    _, status = _git(["status", "--short"], cwd)
    parts = []
    if status.strip():
        parts.append("Changed paths (git status --short):\n" + status.strip())
    if out.strip():
        diff_text = out
        if limit and len(diff_text) > limit:
            diff_text = diff_text[:limit] + f"\n…(diff truncated; {len(out)} chars total)"
        parts.append(f"Diff (`git diff {target}`):\n{diff_text.strip()}")
    if not parts:
        return "", "no uncommitted changes found, so no diff was attached"
    return "\n\n".join(parts), ""


def mask_proxy(value: str) -> str:
    """把代理 URL 里的凭据遮掉，避免回显给模型。"""
    if "@" in value:
        scheme, _, rest = value.partition("://")
        return f"{scheme}://***@{rest.rpartition('@')[2]}" if scheme else f"***@{value.rpartition('@')[2]}"
    return value


def proxy_env_report() -> Dict[str, Any]:
    """报告本服务器进程能看到的代理环境变量（凭据已遮掉）。"""
    report: Dict[str, Any] = {}
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"):
        value = os.environ.get(key)
        if value:
            report[key] = mask_proxy(value) if "PROXY" in key.upper() and "NO_PROXY" not in key.upper() else value
    return report

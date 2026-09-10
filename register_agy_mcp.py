#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 agy-mcp contributors
"""把 agy-mcp 注册进 Codex 配置（这台机器有 cc-switch 的话也一并写进去）。

两处都会写，且都可重复执行：

  1. ~/.codex/config.toml 里的 [mcp_servers.antigravity] 段（写之前先备份并做 TOML 校验）；
  2. 如果这台机器用 cc-switch，则往它的库里也写一份
     （~/.cc-switch/cc-switch.db 的 mcp_servers 表，enabled_codex = 1）。

第 2 步不能省：cc-switch 以自己的数据库为准，会把启用的服务器重新投影到各客户端的
实际配置文件里，否则手写在 config.toml 里的条目会被它覆盖掉。没有 cc-switch 数据库时
这一步自动跳过。

用法：
  python register_agy_mcp.py                 # 注册（可重复执行）
  python register_agy_mcp.py --dry-run       # 只报告会改什么
  python register_agy_mcp.py --remove        # 从两处注销
  python register_agy_mcp.py --id antigravity-b --env AGY_MCP_STATE_DIR=... --env AGY_CLI_HOME=...
                                             # 多账号：第二条条目，各用各的状态目录
  python register_agy_mcp.py --disable-browser
                                             # 顺带拒掉 agy 自带的浏览器工具（改走共享的 Codex 工具）
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from typing import List, Optional

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9 / 3.10
    import tomli as tomllib  # type: ignore[no-redef]

SERVER_ID = "antigravity"
CODEX_TOOLS_ID = "node_repl"
# 拒掉 agy 自带浏览器工具的钩子（见 hooks/deny_browser.py）
BROWSER_HOOK_NAME = "agy-mcp-no-browser"
# 工具名是 CORTEX_STEP_TYPE_* 去掉前缀转小写：browser_* / capture_browser_* /
# click_browser_pixel / execute_browser_javascript / open_browser_url / read_browser_page …
BROWSER_MATCHER = "(?i)(browser|playwright)"
DEFAULT_AGY = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "agy", "bin", "agy.exe"
)


def resolve_default_agy() -> Optional[str]:
    if os.path.exists(DEFAULT_AGY):
        return DEFAULT_AGY
    found = shutil.which("agy") or shutil.which("agy.exe")
    return found


def toml_literal(value: str) -> str:
    """TOML 字面量字符串：反斜杠保持原样（Windows 路径要用）。"""
    if "'" in value:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return "'" + value + "'"


def launch_spec(python_exe: str, script_path: Optional[str]) -> tuple:
    """MCP 条目的 command / args。

    仓库内运行时直接跑脚本文件；`pip install .` 装出来的环境里没有 agy_mcp.py，
    就退回 PATH 上的 `agy-mcp` 命令（console_scripts 入口）。
    """
    if script_path and os.path.exists(script_path):
        return python_exe, [script_path]
    found = shutil.which("agy-mcp") or shutil.which("agy-mcp.exe")
    if found:
        return found, []
    raise SystemExit(
        f"找不到服务器脚本（{script_path}），PATH 上也没有 agy-mcp；"
        "请用 --script 指定 agy_mcp.py，或先 `pip install .`"
    )


def build_block(command: str, args: list, env: dict, server_id: str = SERVER_ID) -> str:
    lines = [
        f"[mcp_servers.{server_id}]",
        'type = "stdio"',
        f"command = {toml_literal(command)}",
        "args = [" + ", ".join(toml_literal(str(arg)) for arg in args) + "]",
        "startup_timeout_sec = 30",
        "tool_timeout_sec = 604800",
    ]
    if env:
        lines.append("")
        lines.append(f"[mcp_servers.{server_id}.env]")
        for key in sorted(env):
            lines.append(f"{key} = {toml_literal(env[key])}")
    return "\n".join(lines) + "\n"


def strip_block(text: str, server_id: str = SERVER_ID) -> str:
    """删掉已有的 [mcp_servers.<id>...] 段，其余内容原样保留。"""
    lines = text.splitlines(keepends=True)
    out: List[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            if stripped.startswith(f"[mcp_servers.{server_id}]") or stripped.startswith(
                f"[mcp_servers.{server_id}."
            ):
                skipping = True
                continue
            skipping = False
        if not skipping:
            out.append(line)
    return "".join(out)


def upsert_block(text: str, block: str) -> str:
    text = strip_block(text)
    if text and not text.endswith("\n"):
        text += "\n"
    if text and not text.endswith("\n\n"):
        text += "\n"
    return text + block


def server_config(command: str, args: list, env: dict) -> dict:
    spec = {
        "type": "stdio",
        "command": command,
        "args": [str(arg) for arg in args],
        "startup_timeout_sec": 30,
        "tool_timeout_sec": 604800,
    }
    if env:
        spec["env"] = dict(env)
    return spec


def write_codex_config(
    path: str, block: str, remove: bool, dry_run: bool, server_id: str = SERVER_ID
) -> str:
    original = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            original = handle.read()

    text = strip_block(original, server_id) if remove else upsert_block(original, block)
    if text == original:
        return "unchanged"

    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"refusing to write invalid TOML to {path}: {exc}")

    if dry_run:
        return "would update"

    if original:
        backup = f"{path}.bak-{time.strftime('%Y%m%d%H%M%S')}"
        with open(backup, "w", encoding="utf-8") as handle:
            handle.write(original)
        print(f"backup: {backup}")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return "updated"


def read_existing_env(path: str, server_id: str = SERVER_ID) -> dict:
    """复用已经注册过的 env 表，这样直接重跑不会把代理设置丢掉。"""
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    servers = data.get("mcp_servers")
    if not isinstance(servers, dict):
        return {}
    entry = servers.get(server_id)
    if not isinstance(entry, dict):
        return {}
    env = entry.get("env")
    if not isinstance(env, dict):
        return {}
    return {str(key): str(value) for key, value in env.items()}


def codex_tool_entries(codex_config: str, server_id: str = SERVER_ID) -> dict:
    """Codex 自带一批工具类 MCP 服务器（带浏览器后端的 node_repl 等）。

    从 Codex 的配置里把它们读出来，agy 就能复用这些工具而不是自己去装
    （自带的 Playwright 驱动现在也下不动了）。
    """
    try:
        with open(codex_config, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    servers = data.get("mcp_servers")
    if not isinstance(servers, dict):
        return {}
    # 跳过我们自己（含另一个 id 的 agy-mcp 条目）：共享出去 agy 就能再调用 agy-mcp，形成递归。
    def is_ours(name: str, entry: dict) -> bool:
        if name in (server_id, SERVER_ID):
            return True
        parts = [str(entry.get("command") or "")] + [str(arg) for arg in entry.get("args") or []]
        return any("agy_mcp.py" in part for part in parts)

    return {
        name: entry
        for name, entry in servers.items()
        if isinstance(entry, dict) and not is_ours(name, entry)
    }


def share_codex_tools(
    entries: dict, chosen: List[str], agy: Optional[str], remove: bool, dry_run: bool
) -> str:
    """把 Codex 自带的工具服务器注册进（或从）Antigravity CLI。"""
    if not agy:
        return "skipped (agy not found)"
    names = chosen or sorted(entries)
    if not names:
        return "skipped (Codex exposes no MCP servers in its config)"
    done = []
    for name in names:
        if remove:
            command = [agy, "mcp", "remove", name]
        else:
            entry = entries.get(name)
            if not entry:
                done.append(f"{name}: not in Codex config")
                continue
            command = [agy, "mcp", "add"]
            for key, value in sorted((entry.get("env") or {}).items()):
                command += ["--env", f"{key}={value}"]
            command.append(name)
            command.append(str(entry.get("command") or ""))
            command += [str(arg) for arg in (entry.get("args") or [])]
        if dry_run:
            print("  would run: " + " ".join(f'"{part}"' if " " in part else part for part in command))
            done.append(f"{name}: dry-run")
            continue
        try:
            proc = subprocess.run(command, capture_output=True, text=True, timeout=90)
        except (OSError, subprocess.SubprocessError) as exc:
            done.append(f"{name}: failed ({exc})")
            continue
        text = (proc.stdout or "").strip() or (proc.stderr or "").strip() or f"exit {proc.returncode}"
        done.append(f"{name}: {text}")
    return "; ".join(done)


def write_db(
    db_path: str, config: dict, remove: bool, dry_run: bool, server_id: str = SERVER_ID
) -> str:
    if not os.path.exists(db_path):
        return "skipped (no cc-switch DB)"
    conn = sqlite3.connect(db_path, timeout=15)
    try:
        cur = conn.cursor()
        if remove:
            affected = cur.execute("DELETE FROM mcp_servers WHERE id = ?1", (server_id,)).rowcount
            if not dry_run:
                conn.commit()
            return "removed" if affected else "unchanged"

        existing = cur.execute(
            "SELECT server_config, enabled_codex FROM mcp_servers WHERE id = ?1", (server_id,)
        ).fetchone()
        payload = json.dumps(config, ensure_ascii=False)
        if existing and existing[1] == 1:
            try:
                stored = json.loads(existing[0])
            except (TypeError, json.JSONDecodeError):
                stored = None
            if stored == config:
                return "unchanged"
        if not dry_run:
            cur.execute(
                """INSERT OR REPLACE INTO mcp_servers
                   (id, name, server_config, description, homepage, docs, tags,
                    enabled_claude, enabled_codex, enabled_gemini, enabled_grokbuild,
                    enabled_opencode, enabled_hermes)
                   VALUES (?1, ?2, ?3, ?4, NULL, NULL, ?5, 0, 1, 0, 0, 0, 0)""",
                (
                    server_id,
                    server_id,
                    payload,
                    "Antigravity CLI (agy) as an MCP tool: one-shot prompts on the signed-in Google account.",
                    json.dumps(["antigravity", "agy", "mcp", "codex"], ensure_ascii=False),
                ),
            )
            conn.commit()
        return "updated" if existing else "inserted"
    finally:
        conn.close()


def default_hooks_path() -> str:
    """agy 的全局 hooks.json：TUI 里的 `/hooks` 命令也写这个文件。"""
    return os.path.join(os.path.expanduser("~"), ".gemini", "config", "hooks.json")


def browser_hook_entry(python_exe: str, hook_script: str, command: Optional[str] = None) -> dict:
    return {
        "PreToolUse": [
            {
                "matcher": BROWSER_MATCHER,
                "hooks": [
                    {
                        "type": "command",
                        "command": command or hook_command(python_exe, hook_script),
                    }
                ],
            }
        ]
    }


def hook_command(python_exe: str, hook_script: str) -> str:
    """钩子命令行。

    agy 用 `cmd /c <command>` 跑它，而 cmd 对"以引号开头的整串"有著名的剥离规则：
    `"C:\\python.exe" "hook.py"` 会被拆坏，直接报"不是内部或外部命令"——更糟的是，
    **钩子命令跑不起来会被当成拒绝**，那一轮就废了。所以 Windows 下不加引号，
    并且安装前必须实测它能跑通（见 `verify_hook_command`）。
    """
    if os.name == "nt":
        return f"{python_exe} {hook_script}"
    return f'"{python_exe}" "{hook_script}"'


def verify_hook_command(command: str) -> tuple:
    """跑一次钩子命令，确认它真能返回 deny 决定；返回 (是否可用, 详情)。"""
    shell = ["cmd", "/c", command] if os.name == "nt" else ["sh", "-c", command]
    try:
        proc = subprocess.run(
            shell,
            input='{"toolCall": {"name": "read_browser_page"}, "stepIdx": 3}',
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run the hook command: {exc}"
    text = (proc.stdout or "").strip()
    try:
        decision = json.loads(text)
    except json.JSONDecodeError:
        detail = (proc.stderr or text or f"exit {proc.returncode}").strip().replace("\n", " ")
        return False, f"hook command did not print JSON: {detail[:200]}"
    if not isinstance(decision, dict) or decision.get("decision") != "deny":
        return False, f"unexpected hook output: {text[:200]}"
    return True, "ok"


def write_browser_hook(
    path: str,
    python_exe: str,
    hook_script: str,
    remove: bool,
    dry_run: bool,
    command: Optional[str] = None,
) -> str:
    """把"拒绝 agy 自带浏览器"的钩子合并进 hooks.json（不动别人写的钩子）。"""
    original = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            original = handle.read()

    hooks: dict = {}
    if original.strip():
        try:
            hooks = json.loads(original)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"refusing to rewrite {path}: it is not valid JSON ({exc})")
        if not isinstance(hooks, dict):
            raise SystemExit(f"refusing to rewrite {path}: the top level is not an object")

    if remove:
        if BROWSER_HOOK_NAME not in hooks:
            return "unchanged"
        hooks.pop(BROWSER_HOOK_NAME)
        if not hooks:
            if not dry_run:
                os.remove(path)
            return "removed"
    else:
        hooks[BROWSER_HOOK_NAME] = browser_hook_entry(python_exe, hook_script, command)

    text = json.dumps(hooks, ensure_ascii=False, indent=2) + "\n"
    if text == original:
        return "unchanged"
    if dry_run:
        return "would update"

    os.makedirs(os.path.dirname(path), exist_ok=True)
    if original:
        backup = f"{path}.bak-{time.strftime('%Y%m%d%H%M%S')}"
        with open(backup, "w", encoding="utf-8") as handle:
            handle.write(original)
        print(f"backup: {backup}")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return "updated"


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description="把 agy-mcp 注册到 cc-switch 与 Codex。")
    parser.add_argument("--python", default=sys.executable, help="跑这个 MCP 服务器用的 Python 解释器。")
    parser.add_argument(
        "--script",
        default=os.path.join(here, "agy_mcp.py"),
        help="agy_mcp.py 的路径（文件不存在时会退回 PATH 上的 agy-mcp 命令）。",
    )
    parser.add_argument(
        "--disable-browser",
        action="store_true",
        help=(
            "给 agy 装一个 PreToolUse 钩子，硬拒它自带的浏览器工具"
            "（playwright 驱动装不上；联网改走共享的 Codex 工具）。配 --remove 卸载。"
        ),
    )
    parser.add_argument(
        "--hooks-file",
        default=None,
        help="hooks.json 路径，默认 ~/.gemini/config/hooks.json。",
    )
    parser.add_argument(
        "--hook-script",
        default=os.path.join(here, "hooks", "deny_browser.py"),
        help="拒绝浏览器用的钩子脚本路径。",
    )
    parser.add_argument(
        "--hook-command",
        default=None,
        help=(
            "直接指定钩子命令（默认由 --python/--hook-script 拼，Windows 下不加引号）。"
            "路径里有空格之类的特殊情况时用它兜底。"
        ),
    )
    parser.add_argument(
        "--id",
        default=SERVER_ID,
        metavar="SERVER_ID",
        help=(
            "MCP 条目的 id（默认 antigravity）。多账号时给第二条换个 id，"
            "再配合 --env AGY_MCP_STATE_DIR/AGY_CLI_HOME 各用各的目录。"
        ),
    )
    parser.add_argument("--agy", default=resolve_default_agy(), help="agy 可执行文件的路径。")
    parser.add_argument(
        "--proxy",
        default=None,
        metavar="URL",
        help="给这个 MCP 服务器设置 HTTP_PROXY/HTTPS_PROXY，例如 http://127.0.0.1:7897。",
    )
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="额外塞给这个 MCP 服务器的环境变量，例如 AGY_MCP_MAX_CALLS_PER_DAY=400。",
    )
    parser.add_argument("--codex-config", default=os.path.join(os.path.expanduser("~"), ".codex", "config.toml"))
    parser.add_argument("--db", default=os.path.join(os.path.expanduser("~"), ".cc-switch", "cc-switch.db"))
    parser.add_argument("--remove", action="store_true", help="注销，而不是注册。")
    parser.add_argument(
        "--clear-env",
        action="store_true",
        help="丢掉已注册的 env 表，而不是往里合并。",
    )
    parser.add_argument(
        "--share-codex-tools",
        nargs="*",
        metavar="NAME",
        help=(
            "把 Codex 自带的 MCP 工具服务器注册进 Antigravity CLI，让 agy 复用 Codex 的工具"
            "（含浏览器）而不是自己装一套。不带名字表示全部；--list-codex-tools 可以看有哪些。"
        ),
    )
    parser.add_argument(
        "--list-codex-tools",
        action="store_true",
        help="列出 Codex 自己跑着的 MCP 服务器（可作为 --share-codex-tools 的候选）。",
    )
    parser.add_argument("--dry-run", action="store_true", help="只报告改动，不写盘。")
    args = parser.parse_args()

    server_id = (args.id or SERVER_ID).strip() or SERVER_ID
    if args.disable_browser and not os.path.exists(args.hook_script):
        raise SystemExit(f"hook script not found: {args.hook_script}")
    if args.disable_browser and args.hook_command:
        # 自定义命令也要先验证：钩子跑不起来会把那一轮直接判死
        ok, detail = verify_hook_command(args.hook_command)
        if not ok:
            raise SystemExit(f"hook command check failed ({detail}); 换个写法再用 --hook-command 传")
    command, argv = launch_spec(args.python, args.script)

    env = {} if (args.clear_env or args.remove) else read_existing_env(args.codex_config, server_id)
    if args.agy:
        env["AGY_BIN"] = args.agy
    if args.proxy:
        env["HTTP_PROXY"] = args.proxy
        env["HTTPS_PROXY"] = args.proxy
        env["NO_PROXY"] = "localhost,127.0.0.1,::1"
    env = {key: value for key, value in env.items() if value}
    for item in args.env:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise SystemExit(f"--env expects KEY=VALUE, got: {item}")
        env[key.strip()] = value

    config = server_config(command, argv, env)
    block = build_block(command, argv, env, server_id)

    print(f"server id      : {server_id}")
    print(f"python         : {args.python}")
    print(f"server launch  : {command} {' '.join(argv)}".rstrip())
    print(f"agy binary     : {args.agy or '(not found - set AGY_BIN later)'}")
    print(f"env            : {', '.join(f'{k}={v}' for k, v in sorted(env.items())) or '(none)'}")
    print(f"codex config   : {args.codex_config} -> {write_codex_config(args.codex_config, block, args.remove, args.dry_run, server_id)}")
    print(f"cc-switch DB   : {args.db} -> {write_db(args.db, config, args.remove, args.dry_run, server_id)}")
    if args.disable_browser:
        hooks_path = args.hooks_file or default_hooks_path()
        hook_cmd = args.hook_command or hook_command(args.python, args.hook_script)
        if not args.remove and not args.dry_run:
            ok, detail = verify_hook_command(hook_cmd)
            if not ok:
                raise SystemExit(
                    f"refusing to install a hook that does not work ({detail})；"
                    "agy 会把跑不起来的钩子当成拒绝，等于把每一轮都判死"
                )
        state = write_browser_hook(
            hooks_path, args.python, args.hook_script, args.remove, args.dry_run, hook_cmd
        )
        print(f"agy hooks      : {hooks_path} -> {state}")
    entries = codex_tool_entries(args.codex_config, server_id)
    if args.list_codex_tools:
        if not entries:
            print("codex tools    : (none found in Codex's config)")
        for name, entry in sorted(entries.items()):
            print(f"codex tool     : {name} -> {entry.get('command')}")
    if args.share_codex_tools is not None or args.remove:
        result = share_codex_tools(
            entries, args.share_codex_tools or [], args.agy, args.remove, args.dry_run
        )
        print(f"agy tools      : {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

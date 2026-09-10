#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 agy-mcp contributors
"""Register the agy-mcp server into a Codex config (and cc-switch, when present).

Two places are written, both idempotently:

  1. the [mcp_servers.antigravity] block in ~/.codex/config.toml (backed up and
     TOML-validated before writing);
  2. if this machine uses cc-switch, the same server in its DB
     (~/.cc-switch/cc-switch.db, mcp_servers table, enabled_codex = 1).

Step 2 matters because cc-switch treats its database as the source of truth and
re-projects the enabled servers into each client's live file, which would
otherwise overwrite a hand-written entry in config.toml. It is skipped when no
cc-switch database exists.

Usage:
  python register_agy_mcp.py                 # register (idempotent)
  python register_agy_mcp.py --dry-run       # show what would change
  python register_agy_mcp.py --remove        # unregister from both places
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
DEFAULT_AGY = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "agy", "bin", "agy.exe"
)


def resolve_default_agy() -> Optional[str]:
    if os.path.exists(DEFAULT_AGY):
        return DEFAULT_AGY
    found = shutil.which("agy") or shutil.which("agy.exe")
    return found


def toml_literal(value: str) -> str:
    """TOML literal string: backslashes stay literal (Windows paths)."""
    if "'" in value:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return "'" + value + "'"


def build_block(python_exe: str, script_path: str, env: dict) -> str:
    lines = [
        f"[mcp_servers.{SERVER_ID}]",
        'type = "stdio"',
        f"command = {toml_literal(python_exe)}",
        f"args = [{toml_literal(script_path)}]",
        "startup_timeout_sec = 30",
        "tool_timeout_sec = 604800",
    ]
    if env:
        lines.append("")
        lines.append(f"[mcp_servers.{SERVER_ID}.env]")
        for key in sorted(env):
            lines.append(f"{key} = {toml_literal(env[key])}")
    return "\n".join(lines) + "\n"


def strip_block(text: str) -> str:
    """Remove an existing [mcp_servers.<id>...] block, keeping the rest intact."""
    lines = text.splitlines(keepends=True)
    out: List[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            if stripped.startswith(f"[mcp_servers.{SERVER_ID}]") or stripped.startswith(
                f"[mcp_servers.{SERVER_ID}."
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


def server_config(python_exe: str, script_path: str, env: dict) -> dict:
    spec = {
        "type": "stdio",
        "command": python_exe,
        "args": [script_path],
        "startup_timeout_sec": 30,
        "tool_timeout_sec": 604800,
    }
    if env:
        spec["env"] = dict(env)
    return spec


def write_codex_config(path: str, block: str, remove: bool, dry_run: bool) -> str:
    original = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            original = handle.read()

    text = strip_block(original) if remove else upsert_block(original, block)
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


def read_existing_env(path: str) -> dict:
    """Reuse the env table already registered so a plain re-run never drops the proxy."""
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    servers = data.get("mcp_servers")
    if not isinstance(servers, dict):
        return {}
    entry = servers.get(SERVER_ID)
    if not isinstance(entry, dict):
        return {}
    env = entry.get("env")
    if not isinstance(env, dict):
        return {}
    return {str(key): str(value) for key, value in env.items()}


def codex_tool_entries(codex_config: str) -> dict:
    """Codex ships its own tool MCP servers (node_repl with browser backends).

    Reading them out of Codex's config lets agy reuse those tools instead of
    installing its own (the bundled Playwright driver no longer downloads).
    """
    try:
        with open(codex_config, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    servers = data.get("mcp_servers")
    if not isinstance(servers, dict):
        return {}
    # Skip our own server: sharing it would let agy call agy-mcp and recurse.
    return {
        name: entry
        for name, entry in servers.items()
        if isinstance(entry, dict) and name != SERVER_ID
    }


def share_codex_tools(
    entries: dict, chosen: List[str], agy: Optional[str], remove: bool, dry_run: bool
) -> str:
    """Register (or drop) Codex's own tool servers inside the Antigravity CLI."""
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


def write_db(db_path: str, config: dict, remove: bool, dry_run: bool) -> str:
    if not os.path.exists(db_path):
        return "skipped (no cc-switch DB)"
    conn = sqlite3.connect(db_path, timeout=15)
    try:
        cur = conn.cursor()
        if remove:
            affected = cur.execute("DELETE FROM mcp_servers WHERE id = ?1", (SERVER_ID,)).rowcount
            if not dry_run:
                conn.commit()
            return "removed" if affected else "unchanged"

        existing = cur.execute(
            "SELECT server_config, enabled_codex FROM mcp_servers WHERE id = ?1", (SERVER_ID,)
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
                    SERVER_ID,
                    SERVER_ID,
                    payload,
                    "Antigravity CLI (agy) as an MCP tool: one-shot prompts on the signed-in Google account.",
                    json.dumps(["antigravity", "agy", "mcp", "codex"], ensure_ascii=False),
                ),
            )
            conn.commit()
        return "updated" if existing else "inserted"
    finally:
        conn.close()


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description="Register agy-mcp with cc-switch and Codex.")
    parser.add_argument("--python", default=sys.executable, help="Python interpreter for the MCP server.")
    parser.add_argument("--script", default=os.path.join(here, "agy_mcp.py"), help="Path to agy_mcp.py.")
    parser.add_argument("--agy", default=resolve_default_agy(), help="Path to the agy executable.")
    parser.add_argument(
        "--proxy",
        default=None,
        metavar="URL",
        help="Set HTTP_PROXY/HTTPS_PROXY for the MCP server, e.g. http://127.0.0.1:7897.",
    )
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Extra environment variable for the MCP server, e.g. AGY_MCP_MAX_CALLS_PER_DAY=400.",
    )
    parser.add_argument("--codex-config", default=os.path.join(os.path.expanduser("~"), ".codex", "config.toml"))
    parser.add_argument("--db", default=os.path.join(os.path.expanduser("~"), ".cc-switch", "cc-switch.db"))
    parser.add_argument("--remove", action="store_true", help="Unregister instead of register.")
    parser.add_argument(
        "--clear-env",
        action="store_true",
        help="Drop the env table already registered instead of merging into it.",
    )
    parser.add_argument(
        "--share-codex-tools",
        nargs="*",
        metavar="NAME",
        help=(
            "Register Codex's own MCP tool servers inside the Antigravity CLI so agy reuses "
            "Codex's tools (browser included) instead of installing its own. No names = all of "
            "them; --list-codex-tools shows what is available."
        ),
    )
    parser.add_argument(
        "--list-codex-tools",
        action="store_true",
        help="List the MCP servers Codex itself runs (candidates for --share-codex-tools).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report changes without writing.")
    args = parser.parse_args()

    if not os.path.exists(args.script):
        raise SystemExit(f"server script not found: {args.script}")

    env = {} if (args.clear_env or args.remove) else read_existing_env(args.codex_config)
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

    config = server_config(args.python, args.script, env)
    block = build_block(args.python, args.script, env)

    print(f"server id      : {SERVER_ID}")
    print(f"python         : {args.python}")
    print(f"server script  : {args.script}")
    print(f"agy binary     : {args.agy or '(not found - set AGY_BIN later)'}")
    print(f"env            : {', '.join(f'{k}={v}' for k, v in sorted(env.items())) or '(none)'}")
    print(f"codex config   : {args.codex_config} -> {write_codex_config(args.codex_config, block, args.remove, args.dry_run)}")
    print(f"cc-switch DB   : {args.db} -> {write_db(args.db, config, args.remove, args.dry_run)}")
    entries = codex_tool_entries(args.codex_config)
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

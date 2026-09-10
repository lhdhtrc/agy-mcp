# 代码结构：core/ 里的模块分工

`agy_mcp.py` 曾经是一个 2600 行的单文件。现在实现都在 `core/` 包里，两个入口脚本
（`main.py` 与兼容壳 `agy_mcp.py`）都只做 `from core.server import main` 的转发。

## 分层

依赖方向自上而下，**下层不认识上层**：

| 模块 | 职责 | 关键名字 |
| --- | --- | --- |
| `config` | 路径、阈值、环境变量解析（唯一读环境变量的地方） | `STATE_DIR`、`MIN_INTERVAL_SEC`、`SERVER_VERSION`、`log` |
| `agy` | 定位并调用 CLI | `resolve_agy`、`agy_command_prefix`、`run_agy`、`_git`、`pid_alive` |
| `protocol` | 纯函数：结果渲染、JSON/流解析、答案提取 | `text_result`、`parse_json_output`、`parse_stream_line`、`extract_answer` |
| `session` | 会话表（按服务器实例隔离）、遗留进程 pid 记录 | `read_sessions`、`write_sessions`、`INSTANCE_ID` |
| `worker` | 常驻会话进程、空闲回收、信号处理、孤儿清理 | `Worker`、`WORKERS`、`reap_orphan_workers` |
| `quota` | 额度读取与解析、后台刷新、配额告警、`model=auto` 选型 | `read_quota`、`resolve_model`、`quota_warning` |
| `jobs` | 后台作业：落盘、脱离进程启动、结果回收 | `JOBS`、`collect_detached_job`、`list_jobs` |
| `prompts` | 提示词拼装：files / diff / no_web / 交接摘要 | `attach_files`、`attach_diff`、`seed_prompt` |
| `guard` | 调用护栏与用量统计、跨进程文件锁、轮次串行化 | `turn_guard`、`read_state`、`log_usage`、`FileLock` |
| `diag` | 本地探测：工作树 diff、代理环境 | `collect_diff`、`proxy_env_report` |
| `tasks` | 异步任务、取消、进度通知、stdio 写出 | `ActiveTask`、`cancel_task`、`notify_progress`、`send` |
| `tools` | 八个工具的 handler 与 schema | `tool_ask`、`tool_submit`、`TOOLS`、`HANDLERS` |
| `server` | MCP 循环、取消/进度接线、`--self-test` 等子命令 | `serve`、`handle_request`、`main` |

为什么 `tasks` 单独一层：`tools` 要发进度、要能被取消，`server` 要收响应，两边都得认识
"当前任务"；把它单独放一层，就不需要 `tools` 与 `server` 互相 import。

`core/__init__.py` 会把常用公开名再导出一遍，所以 `import core` 之后可以直接用
`core.extract_answer(...)`、`core.TOOLS`。模块对象本身也一直可用（`core.session.INSTANCE_ID`）。

## 改代码时的几条纪律

1. **跨模块调用走模块对象**：`from core import session` + `session.read_sessions()`。
   一旦写成 `from core.session import read_sessions` 再直接调用，测试里打在定义模块上的猴补丁
   （`core.session.INSTANCE_ID`、`core.session._is_agy_process`、`core.quota.cached_models`、
   `core.quota._QUOTA_WARNED_AT`）会**静默失效**——测试变绿但没测到东西，比报错更危险。
   只有常量（路径、阈值）可以直接按名导入。
2. **循环依赖用函数内导入**：`agy.run_agy` 要读"当前任务"，而 `tasks` 依赖 `agy`，
   所以那一处保持函数内导入（`from core.tasks import current_task`）。
3. **注释与文档一律中文**（见 [AGENTS.md](../AGENTS.md)），标识符、协议字段名、
   CLI 原样输出、第三方名保留英文。
4. **行为零变化优先**：搬代码只搬代码；想优化另开一次提交，并且先把离线测试跑绿。

## 验收命令

```bash
python3 -m compileall -q core main.py agy_mcp.py register_agy_mcp.py test_agy_mcp.py
python3 test_agy_mcp.py          # 29 项，全绿
python3 agy_mcp.py --list-tools  # 8 个工具
python3 main.py --status         # 走一遍真实 CLI（需要登录过的 agy）
```

`test_agy_mcp.py` 用 `AGY_MCP_AGY_CMD` 注入假 CLI，所以"常驻会话进程 + 多轮协议"也能离线跑；
它直接 `import core`，需要补丁时打在拥有该状态的模块上。

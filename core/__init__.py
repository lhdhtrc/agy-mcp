"""agy-mcp 核心包：按职责分模块，入口是 `core.server.main`。

分层（下层不依赖上层）：

    config   路径、阈值、环境变量解析（唯一读环境变量的地方）
    agy      CLI 定位与调用：resolve_agy / run_agy / _git / pid_alive
    protocol 纯函数：结果渲染、JSON/流解析、答案提取
    session  会话表（按实例隔离）与遗留进程 pid 记录
    worker   常驻会话进程、空闲回收、信号处理、孤儿清理
    quota    额度读取与解析、后台刷新、配额告警、model=auto 选型
    jobs     后台作业：落盘、脱离进程启动、结果回收
    prompts  提示词拼装
    guard    调用护栏与用量统计、跨进程文件锁、轮次串行化
    diag     本地探测：工作树 diff、代理环境
    tasks    异步任务、取消、进度通知、stdio 写出
    tools    八个工具的 handler 与 schema
    server   MCP 服务器循环与 CLI 子命令

这里把常用的公开名再导出一遍，方便 `import core` 之后直接取用；模块名本身也可用
（`core.session.INSTANCE_ID` 这类需要猴补丁的状态，请直接打在拥有它的模块上）。
"""

from core import (  # noqa: F401 —— 让 core.session / core.quota 这类访问总是可用
    agy,
    config,
    diag,
    guard,
    jobs,
    prompts,
    protocol,
    quota,
    server,
    session,
    tasks,
    tools,
    worker,
)
from core.agy import (  # noqa: F401
    agy_command_prefix,
    pid_alive,
    resolve_agy,
    run_agy,
)
from core.config import (  # noqa: F401
    AGY_CLI_HOME,
    AUTO_HANDOFF,
    DEFAULT_MODEL,
    DEFAULT_MODEL_ID,
    DEFAULT_SESSION,
    DEFAULT_TIMEOUT_SEC,
    LONG_CONTEXT_TOKENS,
    MAX_CALLS_PER_DAY,
    MAX_DIFF_CHARS,
    MAX_PARALLEL,
    MAX_PROMPT_CHARS,
    MIN_INTERVAL_SEC,
    PREWARM,
    SERVER_NAME,
    SERVER_VERSION,
    SESSIONS_PATH,
    STATE_DIR,
    TIMEOUT_GRACE_SEC,
    UNLIMITED_PRINT_TIMEOUT,
    WORKER_IDLE_SEC,
)
from core.diag import collect_diff, proxy_env_report  # noqa: F401
from core.guard import merge_usage, read_state, usage_stats, write_state  # noqa: F401
from core.jobs import JOBS, JOBS_DIR, collect_detached_job, list_jobs, read_job  # noqa: F401
from core.prompts import attach_diff, attach_files, attach_no_web, seed_prompt  # noqa: F401
from core.protocol import (  # noqa: F401
    ANSWER_KEYS,
    extract_answer,
    join_streams,
    parse_json_output,
    parse_stream_line,
    progress_from_event,
    summarize_delta,
    text_result,
)
from core.quota import (  # noqa: F401
    MODELS_CACHE,
    QUOTA_CACHE,
    cached_models,
    parse_models,
    quota_warning,
    read_quota,
    reconcile_model_and_effort,
    refresh_quota_in_background,
    resolve_model,
    summarize_quota,
    _QUOTA_WARNED_AT,
)
from core.session import (  # noqa: F401
    INSTANCE_ID,
    newest_conversation_since,
    read_last_conversations,
    read_sessions,
    remember_partial_turn,
    write_sessions,
    _is_agy_process,
    _read_worker_pids,
    _write_worker_pids,
)
from core.tasks import ActiveTask, current_task, is_cancelled, notify_progress, send  # noqa: F401
from core.tools import (  # noqa: F401
    ASK_SCHEMA,
    HANDLERS,
    TOOLS,
    session_flags,
    tool_ask,
    tool_job,
    tool_models,
    tool_quota,
    tool_sessions,
    tool_status,
    tool_submit,
)
from core.worker import WORKERS, Worker, reap_orphan_workers, reap_workers, shutdown_workers  # noqa: F401
from core.server import main  # noqa: F401 —— 两个入口脚本都从这里转发

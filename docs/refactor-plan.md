# 拆分方案：从单文件到包结构

现状：`agy_mcp.py` 约 2600 行、80+ 个顶层定义，已经不好读了。目标不是"重写"，
而是**按职责切成模块，行为一字不改**，每一步都保持离线测试全绿。

## 目标结构

```
agy_mcp/                     # 包（对外仍是同一个入口）
├── __init__.py              # 导出 serve/main，保持 `python -m agy_mcp` 可用
├── __main__.py              # 等价于现在的 `if __name__ == "__main__"`
├── config.py                # 环境变量、路径、超时/阈值等常量（唯一读环境的地方）
├── agy.py                   # CLI 定位与调用：resolve_agy / agy_command_prefix / run_agy / _git
├── worker.py                # Worker 类、WORKERS 注册表、回收线程、信号处理、孤儿清理
├── session.py               # 会话表（按实例隔离）、token 累计、进程 pid 记录
├── quota.py                 # 额度读取与解析、后台刷新、配额告警、模型 auto 选型
├── jobs.py                  # 后台作业：落盘、脱离进程启动、结果回收
├── prompts.py               # 提示词拼装：attach_files / attach_diff / attach_no_web / 交接摘要
├── tools.py                 # 八个工具的 handler（ask/quota/sessions/models/agents/status/submit/job）
├── protocol.py              # MCP 协议层：handle_request / serve / send / 取消与进度
└── server.py                # 组装入口：main()、--self-test、--status、--list-tools
```

根目录保留 `agy_mcp.py`、`register_agy_mcp.py`、`test_agy_mcp.py`、`docs/`、`.github/`。
`agy_mcp.py` 退化为**薄入口**（`from agy_mcp.server import main`），这样既有的客户端配置
（`command = python3 .../agy_mcp.py`）完全不用改。

## 迁移顺序（每步单独提交，每步都必须 29/29 通过）

1. **建包 + 薄入口**：拆出 `config.py` 与 `protocol.py` 里最独立的 `send/handle_request`，
   `agy_mcp.py` 变成转发入口。这一步验证"包结构与旧入口共存"没问题。
2. **抽 `agy.py`**：`resolve_agy` / `agy_command_prefix` / `run_agy` / `_git` / `pid_alive`。
   这一层不含业务状态，风险最低。
3. **抽 `session.py`**：`read_sessions` / `write_sessions` / 实例接管 / token 累计 / `workers.json`。
   注意 `INSTANCE_ID` 是导入期算出来的，抽走后要确保仍只算一次。
4. **抽 `worker.py`**：`Worker` + 注册表 + 回收线程 + 信号处理 + 孤儿清理。
   它与 `session.py`（pid 记录）、`agy.py`（命令前缀）互相依赖，放第四步。
5. **抽 `quota.py` + `prompts.py`**：无状态/纯函数居多，可并行。
6. **抽 `jobs.py`**：作业落盘、脱离进程、结果回收；依赖 `agy.py` + `session.py` + `prompts.py`。
7. **抽 `tools.py`**：八个 handler 集中一处；`tools.py` 只依赖上面各层，不再碰协议细节。
8. **收尾**：`server.py` 只留组装与 CLI 子命令；`agy_mcp.py` 保持薄入口。

## 约束与验收

## 两个必须先解决的硬约束（第一步的关键）

**1. 同名遮蔽**：根目录同时存在 `agy_mcp.py` 与 `agy_mcp/` 包时，`import agy_mcp` 会命中**包**而不是那个文件
（Python 的查找顺序是"包优先于同名模块"）。所以：

- `agy_mcp.py` 只能以脚本身份运行（`python agy_mcp.py`，此时它是 `__main__`），
  **它内部绝不能写 `import agy_mcp`** 来引用自己；
- 脚本里引用包内容必须写全路径（`from agy_mcp.protocol import serve`），这是安全的；
- 反过来，包内模块**不能**去 import 根目录那个 `agy_mcp.py`（会被自己遮蔽）。

**2. 测试是按顶层名字调用的**：`test_agy_mcp.py` 里大量使用 `agy_mcp.resolve_model(...)`、
`agy_mcp.QUOTA_CACHE`、`agy_mcp.session_flags(...)` 这类**顶层名**。一旦 `agy_mcp` 变成包，
这些名字必须由 `agy_mcp/__init__.py` 显式再导出（`from .session import resolve_model, ...`），
否则测试会成片失败。这不是"顺手"，而是第一步必须一起做完的事。

因此第一步的正确形态是：

1. 建 `agy_mcp/` 包，`__init__.py` **把当前根文件里的全部顶层名再导出一遍**（先照搬、后精简）；
2. 把根 `agy_mcp.py` 改成薄脚本：只做 `from agy_mcp.server import main` + `sys.exit(main(sys.argv[1:]))`；
3. 跑三方验收：`python -m py_compile` 全部文件、`python3 test_agy_mcp.py`（29/29）、
   `python3 agy_mcp.py --status`（证明薄入口仍可用）；
4. 这一步**不拆任何逻辑**，只搭骨架——先证明"包 + 薄入口"能共存，再谈后面七步。

- **行为零变化**：只搬代码、改导入，不改逻辑。任何"顺手优化"另开提交。
- **每步都要过**：`python3 -m py_compile`（所有文件）+ `python3 test_agy_mcp.py`（29/29）。
  测试文件保持原样，它本来就是按公开函数名调用的，正好当重构的安全网。
- **协议形状不变**：`tests/fixtures/stream_turn.ndjson` 与 `--self-test` 的形状校验照旧。
- **入口兼容**：`python3 agy_mcp.py --status|--self-test|--list-tools` 与
  `python3 agy_mcp.py`（stdio 服务器）都必须继续可用。
- **注释与文档中文**：见 [AGENTS.md](../AGENTS.md)。
- **发版节奏**：整个拆分做完再打一个 tag（例如 v0.2.0），中间过程不发布。

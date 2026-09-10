# 拆分记录：从单文件到包结构

**状态：已完成（v0.2.0）。** 本文保留当时的计划与纪律；实际结构见 [modules.md](modules.md)。

起点是 `agy_mcp.py` 约 2600 行、80+ 个顶层定义，已经不好读也不好改。目标不是"重写"，
而是**按职责切成模块，行为一字不改**，每一步都保持离线测试全绿。

## 最终结构

```
main.py                      # 正式入口：薄脚本，只做参数转发与 sys.exit(main(...))
agy_mcp.py                   # 兼容壳：同样的薄脚本，保证既有客户端配置不用改
core/                        # 全部实现
├── __init__.py              # 显式再导出全部顶层名（测试与兼容壳都依赖它）
├── config.py                # 环境变量、路径、超时/阈值、服务器与协议常量
├── agy.py                   # CLI 定位与调用：resolve_agy / run_agy / _git / pid_alive
├── protocol.py              # 纯函数：结果渲染、JSON/流解析、答案提取
├── session.py               # 会话表（按实例隔离）、token 累计、进程 pid 记录
├── worker.py                # Worker 类、WORKERS 注册表、回收线程、信号处理、孤儿清理
├── quota.py                 # 额度读取与解析、后台刷新、配额告警、模型 auto 选型
├── jobs.py                  # 后台作业：落盘、脱离进程启动、结果回收
├── prompts.py               # 提示词拼装：files / diff / no_web / 交接摘要
├── guard.py                 # 调用护栏与用量统计、跨进程文件锁、轮次串行化
├── diag.py                  # 本地探测：工作树 diff、代理环境
├── tasks.py                 # 异步任务、取消、进度通知、stdio 写出
├── tools.py                 # 八个工具的 handler 与 schema
└── server.py                # MCP 循环、取消/进度接线、--self-test 等子命令
```

与原计划的差异：**协议循环留在 `server.py`，而 `protocol.py` 只放纯函数**；并新增了
`guard.py` / `diag.py` / `tasks.py` 三个模块。原因是循环依赖——`worker` / `jobs` / `quota`
都要用解析函数，而 `serve` 要用 `worker` 与 `tools`，把两者塞进同一个模块就会成环；
`tasks.py` 的存在则让 `tools` 与 `server` 不必互相 import。

根目录保留 `register_agy_mcp.py`、`test_agy_mcp.py`、`docs/`、`.github/`。

**两个入口都保留**：`main.py` 是正式入口，`agy_mcp.py` 是兼容壳——既有客户端配置里写的是
`python3 .../agy_mcp.py`，它必须继续可用。两者内容一致，都只做转发生效。

好处：包名 `core` 与入口文件名不再冲突（原来的 `agy_mcp/` + `agy_mcp.py` 会触发"包优先于
同名模块"的遮蔽问题）。

## 实际走的顺序（每步一次提交，每步 29/29 通过）

| 步骤 | 内容 |
| --- | --- |
| 1 | 建 `core/` 包 + 薄入口，实现先整包搬进 `core/impl.py`，验证"包 + 旧入口"共存 |
| 2 | 抽 `agy.py`（CLI 定位与调用） |
| 3 | 抽 `config.py`（路径与常量）与 `session.py`（会话表、pid 记录） |
| 4 | 抽 `worker.py`（常驻进程与回收） |
| 5 | 抽 `jobs.py`（后台作业）与 `prompts.py`（提示词拼装） |
| 6 | 抽 `quota.py`（额度与选型） |
| 7 | 抽 `protocol.py` 的纯函数（渲染 / 解析 / 答案提取），顺手清掉各模块里的临时懒加载 |
| 8 | 抽 `guard.py` / `diag.py` / `tasks.py`，再抽 `tools.py`（八个 handler）与 `server.py`（协议循环与入口） |
| 9 | 删除 `core/impl.py`：两个入口改为 `from core.server import main`，测试改 `import core` |

## 验收与纪律（仍然有效）

- **行为零变化**：只搬代码、改导入，不改逻辑。任何"顺手优化"另开提交。
- **每步都要过**：`python3 -m compileall -q core main.py agy_mcp.py register_agy_mcp.py test_agy_mcp.py`
  以及 `python3 test_agy_mcp.py`（29 项，全绿）。
- **协议形状不变**：`tests/fixtures/stream_turn.ndjson` 与 `--self-test` 的形状校验照旧。
- **入口兼容**：`python3 agy_mcp.py --status|--self-test|--list-tools` 与
  `python3 agy_mcp.py`（stdio 服务器）都必须继续可用。
- **注释与文档中文**：见 [AGENTS.md](../AGENTS.md)。
- **发版节奏**：整个拆分做完再打 tag（v0.2.0），中间过程不发布。

## 三个踩过的坑

**1. 测试是按顶层名字调用的。** `test_agy_mcp.py` 里有 24 个 `agy_mcp.xxx` 顶层引用，
拆分后由 `core/__init__.py` 显式再导出；测试改成 `import core as agy_mcp`，
需要补丁的状态（`INSTANCE_ID`、`_is_agy_process`、`cached_models`、`_QUOTA_WARNED_AT`）
直接打在拥有它的模块上（`core.session` / `core.quota`）。

**2. 入口脚本不要自引用。** `main.py` 与 `agy_mcp.py` 只能写 `from core.server import main`——
绝不能写 `import agy_mcp` 去引用自己（同名遮蔽问题已经用 `core` 包名绕开，但习惯要守住）。

**3. 搬代码时最危险的是"漏导入"。** `jobs.py`、`quota.py` 里有几处只在兜底路径上才会用到的
名字（`pid_alive`、`read_sessions`、`join_streams`），离线测试全绿也照样是坏的。
收尾时用 `pyflakes` 扫了一遍 `undefined name`，并手工验证了那些路径（配额读失败、
作业结果回收）确实能走通。

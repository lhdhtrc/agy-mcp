# AGENTS.md — agy-mcp 协作约定

## 语言（硬性要求）

**注释与文档一律中文**，包括：

- 代码注释、docstring、CLI `--help` 文案；
- `README.md`、`docs/**`、issue/PR 模板等一切文档；
- 提交信息（commit message）与代码评审回复。

英文只保留在不可避免的地方：标识符、协议字段名（如 `notifications/progress`）、CLI 原样输出、
第三方文件名与 URL。**新增或修改注释/文档时，先检查语言是否中文。**

## 项目约定

- 入口文档：[README.md](README.md)；详细文档在 [docs/](docs/README.md)。
- **代码结构**：实现都在 `core/` 包里，`main.py` 是正式入口，`agy_mcp.py` 是同内容的兼容壳，
  两者都只做 `from core.server import main`。模块职责与依赖方向见
  [docs/modules.md](docs/modules.md)；新增功能前先看它决定放哪一层。
- **跨模块调用走模块对象**（`from core import session` + `session.read_sessions()`），
  否则测试打在定义模块上的猴补丁会静默失效；只有常量可以按名导入。
- 改代码必须保持离线测试通过：
  `python3 -m compileall -q core main.py agy_mcp.py register_agy_mcp.py test_agy_mcp.py` 与
  `python3 test_agy_mcp.py`（29+ 项，不需要网络、账号或 agy）。
- 涉及协议（`--input-format stream-json` 的事件形状）的改动，同步更新
  `tests/fixtures/stream_turn.ndjson` 和 `--self-test` 的形状校验。
- 版本发布：推 `v*` tag 触发 `release.yml`（先跑测试再发版）。改版本号只改
  `core/config.py` 的 `SERVER_VERSION` 与 README 顶部那行。
- 总原则：**agy 复用 Codex 的能力（工具、浏览器、后续插件），只有推理用 agy 自己的模型。**

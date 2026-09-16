# 历史设计文档

本目录保存 Python 版本时期的调研、设计规格与实施计划，**仅作历史记录**。

其中的代码路径（`src/qb2api/...`）、模块名与命令均已不存在：当前实现是 Go 版本
（`cmd/`、`internal/`），Python 后端已在 `python-version` 分支保留。

阅读时的映射关系：

| 历史文档中的位置 | 现在的对应位置 |
| --- | --- |
| `src/qb2api/worker/` | `internal/providers/`、`internal/server/plane.go` |
| `src/qb2api/control/` | `internal/server/`（单进程，不再有 control/worker 拆分） |
| `src/qb2api/checkin/` | `internal/checkin/` |
| `docs/analysis/` | `docs/history/analysis/`（上游协议调研，同样为历史记录） |
| `src/qb2api/admin/` | `internal/server/admin_*.go` |
| `src/qb2api/web/dist` | `web/dist` |
| `tests/` | `internal/**/*_test.go` |

行为契约（API 响应形状、状态枚举、时间戳格式、百分位口径）在这些文档成文后仍然有效，
详见 [../implementation-notes.md](../implementation-notes.md)。

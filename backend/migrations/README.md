# migrations

当前版本用 SQLAlchemy `Base.metadata.create_all()` 在 backend 启动时建表（小型项目，无需迁移框架）。

- `0001_initial.sql`：与 `src/models/*.py` 一致的 MySQL 8.0 DDL 快照，供 review 与手工核查使用，**不参与启动**。
- `0002_probe_run_checkpoint.sql`：巡检批次状态机 / 断点续跑 / 发布闸门的增量 DDL（`probe_runs` 加列 + 新建 `probe_run_items`），供**已有数据卷**手工升级；新卷由 `create_all` 直接建成最新结构，无需执行。
- 结构变更流程（约定）：改 `src/models/` → 同步改本目录新增 `000N_*.sql` → 在 PR 说明里写清影响面。

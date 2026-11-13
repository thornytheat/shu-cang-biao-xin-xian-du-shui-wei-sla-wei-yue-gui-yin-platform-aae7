-- 0002：巡检批次状态机 / 断点续跑 / 发布闸门
-- 运行时仍由 SQLAlchemy Base.metadata.create_all 建表；本文件供已有数据卷手工升级，
-- 与 src/models/probe_run.py、src/models/probe_run_item.py 字段一一对应，不参与容器启动。
-- 注意：MySQL 8.0 的 ALTER TABLE ... ADD COLUMN 不支持 IF NOT EXISTS，重复执行会报重复列，属正常。

-- 1) probe_runs 由"每日一行计数"升级为"批次状态机 + 断点 + 发布时间"
ALTER TABLE probe_runs
  ADD COLUMN status VARCHAR(12) NOT NULL DEFAULT 'running' COMMENT 'running/published/failed' AFTER run_date,
  ADD COLUMN total_tables INT NOT NULL DEFAULT 0 AFTER trigger_source,
  ADD COLUMN completed_tables INT NOT NULL DEFAULT 0 AFTER total_tables,
  ADD COLUMN failed_tables INT NOT NULL DEFAULT 0 AFTER completed_tables,
  ADD COLUMN last_table_code VARCHAR(80) NULL AFTER failed_tables,
  ADD COLUMN fail_reason VARCHAR(255) NULL AFTER duration_ms,
  ADD COLUMN last_error TEXT NULL AFTER fail_reason,
  ADD COLUMN heartbeat_at DATETIME NULL AFTER last_error,
  ADD COLUMN started_at DATETIME NULL AFTER heartbeat_at,
  ADD COLUMN finished_at DATETIME NULL AFTER started_at,
  ADD COLUMN published_at DATETIME NULL AFTER finished_at,
  ADD COLUMN updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP AFTER created_at;

-- 历史已存在的批次行视为"已发布"，避免升级后旧数据被看板发布闸门隐藏
UPDATE probe_runs SET status = 'published', published_at = COALESCE(created_at, NOW()) WHERE status = 'running';

CREATE INDEX idx_probe_runs_status ON probe_runs (status);

-- 2) 批次内单表采集明细（断点续跑 / 不重复写入的落点）
CREATE TABLE IF NOT EXISTS probe_run_items (
  id             INT AUTO_INCREMENT PRIMARY KEY,
  run_id         INT NOT NULL,
  table_id       INT NOT NULL,
  table_code     VARCHAR(80) NOT NULL,
  ordinal        INT NOT NULL DEFAULT 0 COMMENT '批次内顺序，从 0 开始',
  status         VARCHAR(12) NOT NULL DEFAULT 'pending' COMMENT 'pending/done/failed',
  attempts       INT NOT NULL DEFAULT 0,
  error_message  TEXT NULL,
  started_at     DATETIME NULL,
  finished_at    DATETIME NULL,
  created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_run_item_table (run_id, table_id),
  KEY idx_run_item_run (run_id),
  KEY idx_run_item_table (table_id),
  KEY idx_run_item_status (status),
  CONSTRAINT fk_run_item_run FOREIGN KEY (run_id) REFERENCES probe_runs(id) ON DELETE CASCADE,
  CONSTRAINT fk_run_item_table FOREIGN KEY (table_id) REFERENCES table_assets(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

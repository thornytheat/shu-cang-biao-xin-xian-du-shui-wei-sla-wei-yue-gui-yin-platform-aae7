import { RUN_STATUS_LABELS } from '../../constants/enums';
import type { RunStatus } from '../../types/models';

/** 巡检批次状态徽标：进行中（黄）/ 已发布（绿）/ 失败（红）。 */
export function RunStatusBadge({ status, label }: { status: RunStatus; label?: string }) {
  const text = label ?? RUN_STATUS_LABELS[status] ?? status;
  const cls =
    status === 'published' ? 'run-badge run-published' : status === 'failed' ? 'run-badge run-failed' : 'run-badge run-running';
  return (
    <span className={cls} data-status={status}>
      <i className="run-dot" aria-hidden="true" />
      {text}
    </span>
  );
}

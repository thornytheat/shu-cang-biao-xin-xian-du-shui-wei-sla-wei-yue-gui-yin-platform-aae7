import { RunStatusBadge } from './RunStatusBadge';
import { PROBE_ITEM_STATUS_LABELS } from '../../constants/enums';
import type { ProbeRunResult } from '../../types/models';

interface RunBannerProps {
  run: ProbeRunResult;
  /** 进行中/失败时显示"续跑"按钮；为 true 时按钮置忙 */
  busy?: boolean;
  onResume?: () => void;
  /** 是否展开失败表明细（默认失败时展开） */
  showItems?: boolean;
}

/** 批次状态横幅：进行中显示断点进度，失败显示原因与续跑入口，已发布给出整批结论。 */
export function RunBanner({ run, busy = false, onResume, showItems }: RunBannerProps) {
  const bannerCls =
    run.status === 'published'
      ? 'run-banner banner-published'
      : run.status === 'failed'
        ? 'run-banner banner-failed'
        : 'run-banner banner-running';
  const canResume = run.status !== 'published' && !!onResume;
  const failedItems = (run.items ?? []).filter((item) => item.status === 'failed');
  const expandItems = showItems ?? run.status === 'failed';

  return (
    <div className={bannerCls} role="status" aria-live="polite">
      <div className="run-banner-head">
        <span className="run-banner-title">巡检批次 · {run.run_date}</span>
        <RunStatusBadge status={run.status} label={run.status_label} />
        <span className="run-banner-sub">
          触发 {run.triggered_by}（{run.trigger_source}） · 断点 {run.last_table_code ?? '—'}
        </span>
        <span className="spacer" style={{ flex: 1 }} />
        {canResume ? (
          <button type="button" className="btn btn-primary" onClick={onResume} disabled={busy}>
            {busy ? '续跑中…' : run.status === 'failed' ? '从断点续跑' : '继续巡检'}
          </button>
        ) : null}
      </div>

      <div className="run-progress">
        <div className="run-progress-track">
          <div className="run-progress-fill" style={{ width: `${run.progress_pct}%` }} />
        </div>
        <span className="run-progress-num num">
          {run.completed_tables}/{run.total_tables}（{run.progress_pct}%）
          {run.failed_tables > 0 ? ` · 失败 ${run.failed_tables}` : ''}
        </span>
      </div>

      {run.status === 'failed' && run.fail_reason ? (
        <div className="run-fail-reason">挂起原因：{run.fail_reason}（已完成的探测已保留，续跑不会重复采集）</div>
      ) : null}

      {run.status === 'published' ? (
        <div className="run-banner-sub">
          整批已发布：缺失 {run.missing_count} · 迟到 {run.late_count} · 红灯 {run.red_count} 张
          {run.published_at ? ` · 发布于 ${run.published_at}` : ''}
        </div>
      ) : null}

      {expandItems && failedItems.length > 0 ? (
        <div className="run-item-list">
          {failedItems.map((item) => (
            <div className="run-item-row" key={item.table_code}>
              <span className="code num">{item.table_code}</span>
              <span className="tag" style={{ color: 'var(--lamp-red)' }}>
                {PROBE_ITEM_STATUS_LABELS[item.status]}
              </span>
              <span className="num run-banner-sub">尝试 {item.attempts} 次</span>
              {item.error_message ? <span className="run-item-err">{item.error_message}</span> : null}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

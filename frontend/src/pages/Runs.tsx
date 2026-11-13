import { useState } from 'react';
import { probesApi } from '../api/probes';
import { EmptyState, ErrorState, LoadingBlock } from '../components/common/EmptyState';
import { RunBanner } from '../components/common/RunBanner';
import { RunStatusBadge } from '../components/common/RunStatusBadge';
import { RUN_STATUS_LABELS } from '../constants/enums';
import { useAsync } from '../hooks/useAsync';
import { useToasts } from '../hooks/useToast';
import type { ProbeRunResult } from '../types/models';
import { shortDateTime } from '../utils/format';

/** 巡检批次历史：每个业务日一条，标明进行中 / 已发布 / 失败，可展开断点明细、失败可续跑。 */
export function Runs() {
  const { toast } = useToasts();
  const state = useAsync(() => probesApi.runs(30), []);
  const [openId, setOpenId] = useState<number | null>(null);
  const [detail, setDetail] = useState<ProbeRunResult | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [resuming, setResuming] = useState(false);

  const items = state.data?.items ?? [];

  async function toggle(run: ProbeRunResult) {
    if (openId === run.id) {
      setOpenId(null);
      setDetail(null);
      return;
    }
    setOpenId(run.id);
    setDetail(null);
    setLoadingDetail(true);
    try {
      const full = await probesApi.runDetail(run.id);
      setDetail(full);
    } catch (err) {
      toast(err instanceof Error ? err.message : '批次明细加载失败', 'error');
    } finally {
      setLoadingDetail(false);
    }
  }

  async function onResume() {
    setResuming(true);
    try {
      const result = await probesApi.run();
      if (result.status === 'published') {
        toast(`巡检 ${result.run_date} 已发布：完成 ${result.completed_tables}/${result.total_tables}`, 'ok');
      } else if (result.status === 'failed') {
        toast(`仍挂起于 ${result.last_table_code ?? '—'}：${result.fail_reason ?? '采集失败'}`, 'error');
      } else {
        toast(`巡检进行中：${result.completed_tables}/${result.total_tables}`, 'ok');
      }
      state.reload();
      if (openId !== null) {
        const full = await probesApi.runDetail(openId);
        setDetail(full);
      }
    } catch (err) {
      toast(err instanceof Error ? err.message : '续跑失败', 'error');
    } finally {
      setResuming(false);
    }
  }

  if (state.error) {
    return <ErrorState message={state.error} onRetry={state.reload} hint="后端可能刚重启，稍后重试。" />;
  }
  if (state.loading && !state.data) {
    return (
      <section className="section">
        <LoadingBlock rows={6} />
      </section>
    );
  }

  return (
    <>
      <section className="section">
        <div className="section-head">
          <h2>巡检批次</h2>
          <span className="count num">{state.data?.total ?? 0} 个批次</span>
        </div>
        <p className="run-banner-sub" style={{ margin: '6px 0 14px' }}>
          每个业务日至多一个批次：进行中可看断点进度，失败可从最后完成位置续跑（不重复采集），整批校验通过后才置为已发布并同步看板 / 表详情 / 违约台账。
        </p>

        {items.length === 0 ? (
          <EmptyState title="还没有巡检批次" body="去看板点「运行今日探测」建立第一个批次。" />
        ) : (
          <div className="ledger-wrap">
            <table className="ledger">
              <thead>
                <tr>
                  <th>业务日</th>
                  <th>状态</th>
                  <th>进度（断点）</th>
                  <th>缺失 / 迟到 / 红灯</th>
                  <th>触发</th>
                  <th>开始 / 发布</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {items.map((run) => (
                  <tr key={run.id} className={run.status === 'failed' ? 'row-failed' : ''}>
                    <td className="num">{run.run_date}</td>
                    <td>
                      <RunStatusBadge status={run.status} label={RUN_STATUS_LABELS[run.status]} />
                    </td>
                    <td className="num">
                      {run.completed_tables}/{run.total_tables}（{run.progress_pct}%）
                      {run.failed_tables > 0 ? <span style={{ color: 'var(--lamp-red)' }}> · 失败 {run.failed_tables}</span> : null}
                    </td>
                    <td className="num">
                      {run.missing_count} / {run.late_count} / {run.red_count}
                    </td>
                    <td>
                      {run.triggered_by}
                      <br />
                      <span className="run-banner-sub">{run.trigger_source}</span>
                    </td>
                    <td className="num" style={{ fontSize: 12 }}>
                      {shortDateTime(run.started_at)}
                      <br />
                      <span className="run-banner-sub">{shortDateTime(run.published_at)}</span>
                    </td>
                    <td>
                      <button type="button" className="btn btn-ghost" onClick={() => toggle(run)}>
                        {openId === run.id ? '收起' : '明细'}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {openId !== null && detail ? (
        <section className="section">
          <RunBanner run={detail} busy={resuming} onResume={detail.status === 'published' ? undefined : onResume} />
          {detail.fail_reason ? <div className="run-fail-reason">最近失败：{detail.fail_reason}</div> : null}
        </section>
      ) : null}
      {openId !== null && loadingDetail ? (
        <section className="section">
          <LoadingBlock rows={3} />
        </section>
      ) : null}
    </>
  );
}

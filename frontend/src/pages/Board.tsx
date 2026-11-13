import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { breachesApi } from '../api/breaches';
import { probesApi } from '../api/probes';
import { tablesApi } from '../api/tables';
import { EmptyState, ErrorState, LoadingBlock } from '../components/common/EmptyState';
import { RunBanner } from '../components/common/RunBanner';
import { RunStatusBadge } from '../components/common/RunStatusBadge';
import { TierLamp } from '../components/common/TierLamp';
import { WaterBar } from '../components/common/WaterBar';
import { GRADE_HINTS, LAYER_LABELS, TIER_LABELS, TIER_ORDER } from '../constants/enums';
import { can } from '../constants/roles';
import { useAsync } from '../hooks/useAsync';
import { useAuth } from '../hooks/useAuth';
import { useToasts } from '../hooks/useToast';
import type { BoardRow, Grade, ProbeRunResult } from '../types/models';
import { describeError, permissionHint } from '../utils/errorMessage';
import { formatMinutes, formatRatio, trimZero } from '../utils/format';

const GRADE_ORDER: Grade[] = ['red', 'yellow', 'green', 'unknown'];
/** 进行中/失败批次的轮询间隔：让值班看到断点实时推进，发布后自动停止 */
const RUN_POLL_MS = 3000;

/** 表健康看板：P0/P1/P2 三栏 + 水位条 + 三色灯；巡检按批次断点续跑、整批发布。 */
export function Board() {
  const { user } = useAuth();
  const { toast } = useToasts();
  const [layer, setLayer] = useState('');
  const [keyword, setKeyword] = useState('');
  const [runResult, setRunResult] = useState<ProbeRunResult | null>(null);
  const [running, setRunning] = useState(false);

  const boardState = useAsync(() => tablesApi.board({ layer: layer || undefined, q: keyword || undefined }), [layer, keyword]);
  const summaryState = useAsync(() => breachesApi.summary(), []);
  const statusState = useAsync(() => probesApi.status(), []);

  const board = boardState.data;
  const canRun = can(user?.role, 'probe:run');

  const totals = board?.totals;
  const breachSummary = summaryState.data;
  const currentRun = runResult ?? statusState.data?.run ?? null;
  // 仅"进行中"才轮询（批次在推进）；failed 是等人工续跑的稳定态，不做无意义轮询
  const isRunAdvancing = currentRun?.status === 'running';

  const runLabel = useMemo(() => {
    if (running) return '续跑中…';
    if (currentRun?.status === 'failed') return '从断点续跑';
    if (currentRun?.status === 'running') return '巡检进行中';
    return '运行今日探测';
  }, [running, currentRun?.status]);

  // 批次进行中轮询状态；发布/失败后自动停止，发布时刷新看板/台账
  useEffect(() => {
    if (!isRunAdvancing) return undefined;
    const timer = setInterval(() => {
      probesApi
        .status()
        .then((payload) => {
          if (payload.run) setRunResult(payload.run);
          if (payload.run?.status === 'published') {
            boardState.reload();
            summaryState.reload();
            statusState.reload();
          }
        })
        .catch(() => undefined);
    }, RUN_POLL_MS);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isRunAdvancing, currentRun?.status]);

  async function onRun() {
    setRunning(true);
    try {
      const result = await probesApi.run();
      setRunResult(result);
      if (result.status === 'published') {
        toast(
          result.idempotent
            ? `巡检 ${result.run_date} 已发布（重复触发，未重复采集）`
            : `巡检已发布：完成 ${result.completed_tables}/${result.total_tables}，红灯 ${result.red_count} 张`,
          'ok',
        );
        boardState.reload();
        summaryState.reload();
      } else if (result.status === 'failed') {
        toast(`批次挂起于 ${result.last_table_code ?? '—'}：${result.fail_reason ?? '采集失败'}，可从断点续跑`, 'error');
      } else {
        toast(`巡检进行中：已完成 ${result.completed_tables}/${result.total_tables}`, 'ok');
      }
      statusState.reload();
    } catch (err) {
      toast(`${describeError(err)}｜${permissionHint('probe:run')}`, 'error');
    } finally {
      setRunning(false);
    }
  }

  return (
    <>
      <section className="section">
        <div className="section-head">
          <h2>今日巡检</h2>
          <span className="count num">
            {statusState.data?.today ?? '—'} ·{' '}
            {statusState.data?.probed_today ? '已发布' : currentRun ? `${currentRun.status_label}` : '未巡检'}
          </span>
          <span className="spacer" style={{ flex: 1 }} />
          <div className="inline-actions">
            <select value={layer} onChange={(event) => setLayer(event.target.value)} aria-label="按分层筛选">
              <option value="">全部层（ODS/DWD/DWS/ADS）</option>
              {Object.entries(LAYER_LABELS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
            <input
              type="text"
              placeholder="搜表名 / 中文名"
              value={keyword}
              onChange={(event) => setKeyword(event.target.value)}
              style={{ width: 190 }}
              aria-label="搜索表"
            />
            <button
              type="button"
              className={canRun ? 'btn btn-primary' : 'btn'}
              onClick={onRun}
              disabled={running || currentRun?.status === 'running'}
              title={canRun ? '跑一次 40 张表的巡检；失败可从断点续跑，整批通过后统一发布' : `${permissionHint('probe:run')}（点击可验证后端 403）`}
            >
              {runLabel}
            </button>
          </div>
        </div>

        <div className="inline-actions" style={{ fontSize: 12, color: 'var(--ink-400)' }}>
          <span className="num">水线 {board?.waterline ?? '—'}</span>
          <span>·</span>
          <span>最近发布 {board?.latest_published ?? '—'}</span>
          <span>·</span>
          <span>分位基线窗口 {board?.calibration_days ?? 30} 天</span>
          <span>·</span>
          <span>违约 {breachSummary?.total ?? '—'} 起（严重 {breachSummary?.severe_total ?? '—'} 起）</span>
          <span>·</span>
          <span>红灯 {totals?.red ?? '—'} 张 / 待认领 {breachSummary?.by_status?.open ?? '—'} 起</span>
          {currentRun && currentRun.status !== 'published' ? (
            <RunStatusBadge status={currentRun.status} label={`本批${currentRun.status_label}：看板仅展示已发布数据`} />
          ) : null}
          {!canRun ? (
            <span className="tag" style={{ color: 'var(--lamp-yellow)' }}>
              当前角色只读：探测由 admin/ops 触发
            </span>
          ) : null}
        </div>

        {currentRun ? <RunBanner run={currentRun} busy={running} onResume={canRun ? onRun : undefined} /> : null}
      </section>

      {boardState.error ? (
        <ErrorState
          message={boardState.error}
          onRetry={boardState.reload}
          hint="若刚启动容器，等 backend healthy 后再刷新；也可直接点重试。"
        />
      ) : null}

      {boardState.loading && !board ? (
        <section className="section">
          <LoadingBlock rows={6} />
        </section>
      ) : null}

      {board ? (
        <>
          <section className="section" style={{ gap: 10 }}>
            <div className="stat-grid five">
              <div className="stat">
                <div className="k">核心表</div>
                <div className="v">{totals?.tables ?? 0}</div>
              </div>
              <div className="stat">
                <div className="k">红灯（&gt; P90）</div>
                <div className="v" style={{ color: 'var(--lamp-red)' }}>
                  {totals?.red ?? 0}
                </div>
              </div>
              <div className="stat">
                <div className="k">黄灯</div>
                <div className="v" style={{ color: 'var(--lamp-yellow)' }}>
                  {totals?.yellow ?? 0}
                </div>
              </div>
              <div className="stat">
                <div className="k">绿灯</div>
                <div className="v" style={{ color: 'var(--lamp-green)' }}>
                  {totals?.green ?? 0}
                </div>
              </div>
              <div className="stat">
                <div className="k">缺失（unknown）</div>
                <div className="v" style={{ color: 'var(--ink-300)' }}>
                  {totals?.unknown ?? 0}
                </div>
              </div>
            </div>
          </section>

          <div className="board-grid">
            {TIER_ORDER.map((tier) => {
              const column = board.tiers[tier];
              if (!column) return null;
              return (
                <section className="tier-col" key={tier}>
                  <header className="tier-col-head">
                    <span className="tier-name">{TIER_LABELS[tier].split(' · ')[0]}</span>
                    <span className="tier-sub">{TIER_LABELS[tier].split(' · ')[1]} · 容忍 {column.tolerance_minutes}m</span>
                    <span className="counters num">
                      {GRADE_ORDER.map((grade) => (
                        <span key={grade} style={{ color: gradeColor(grade) }}>
                          {countersText(grade)} {column.counters[grade] ?? 0}
                        </span>
                      ))}
                    </span>
                  </header>
                  <div className="rowlist">
                    {column.rows.length === 0 ? (
                      <div style={{ padding: 14 }}>
                        <EmptyState title="这一栏没有命中记录" body="换个分层或清空搜索词再看。" />
                      </div>
                    ) : (
                      column.rows.map((row) => <BoardRowItem key={row.code} row={row} />)
                    )}
                  </div>
                </section>
              );
            })}
          </div>

          <div className="legend">
            {GRADE_ORDER.map((grade) => (
              <span key={grade} className="inline-actions">
                <TierLamp grade={grade} />
                <span>{GRADE_HINTS[grade]}</span>
              </span>
            ))}
            <span style={{ marginLeft: 'auto' }}>水位条满格 = 延迟达到容忍值的 2 倍；竖线 = 容忍线</span>
          </div>
        </>
      ) : null}

      {board && totals?.tables === 0 ? (
        <EmptyState
          title="没有匹配的表"
          body="当前筛选条件下 40 张核心表都没命中。清空搜索词或把分层切回“全部层”。"
          action={
            <button
              type="button"
              className="btn"
              onClick={() => {
                setLayer('');
                setKeyword('');
              }}
            >
              清空筛选
            </button>
          }
        />
      ) : null}
    </>
  );
}

function BoardRowItem({ row }: { row: BoardRow }) {
  const delayText =
    row.delay_minutes === null ? '缺失' : `${trimZero(row.delay_minutes)}`;
  return (
    <div className={`trow ${row.grade}`}>
      <TierLamp grade={row.grade} />
      <div style={{ minWidth: 0 }}>
        <div className="code">
          {row.code}
          {row.is_early ? <span style={{ color: 'var(--lamp-green)', fontSize: 10, marginLeft: 6 }}>early</span> : null}
        </div>
        <div className="name">
          {row.name} · {row.report_count} 张报表 · {row.expected_ready_clock} 应就绪
        </div>
        <WaterBar ratio={row.ratio} grade={row.grade} toleranceMinutes={row.tolerance_minutes} />
      </div>
      <div className="delay">
        {delayText}
        <small>
          {row.ratio === null ? '探测缺失' : `${formatRatio(row.ratio)} / ${row.tolerance_minutes}m`}
        </small>
        <small style={{ color: 'var(--ink-600)' }}>
          {row.delay_minutes === null ? '—' : formatMinutes(row.delay_minutes)}
        </small>
      </div>
      <Link className="rowlink" to={`/tables/${encodeURIComponent(row.code)}`} aria-label={`查看 ${row.code} 详情`} />
    </div>
  );
}

function gradeColor(grade: Grade): string {
  if (grade === 'red') return 'var(--lamp-red)';
  if (grade === 'yellow') return 'var(--lamp-yellow)';
  if (grade === 'green') return 'var(--lamp-green)';
  return 'var(--ink-400)';
}

function countersText(grade: Grade): string {
  return { red: '红', yellow: '黄', green: '绿', unknown: '缺' }[grade];
}

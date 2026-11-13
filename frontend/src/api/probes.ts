import { api } from './client';
import type { ProbeRunList, ProbeRunResult, ProbeStatusPayload } from '../types/models';

export interface ProbeRow {
  id: number;
  table_code: string;
  table_name: string;
  tier: string;
  probe_date: string;
  partition_dt: string;
  expected_ready_at: string;
  actual_ready_at: string | null;
  delay_minutes: number | null;
  status: string;
  is_early: boolean;
}

export const probesApi = {
  /** 触发/续跑今日巡检：同日重复调用不新增批次，已发布则幂等返回 */
  run: () => api.post<ProbeRunResult>('/api/probes/run'),
  status: () => api.get<ProbeStatusPayload>('/api/probes/status'),
  runs: (limit = 20) => api.get<ProbeRunList>(`/api/probes/runs?limit=${limit}`),
  runDetail: (runId: number) => api.get<ProbeRunResult>(`/api/probes/runs/${runId}`),
  list: (table?: string, days = 14) => {
    const search = new URLSearchParams({ days: String(days) });
    if (table) search.set('table', table);
    return api.get<{ items: ProbeRow[]; total: number }>(`/api/probes?${search.toString()}`);
  },
};

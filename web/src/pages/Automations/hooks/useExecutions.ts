import { useCallback } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { queryKeys } from '@/lib/queryKeys';
import type { AutomationExecution } from '@/types/automation';
import { isRunLive } from '../utils/status';
import { pollMs } from '../utils/polling';
import { listExecutions } from '../utils/api';

interface UseExecutionsResult {
  executions: AutomationExecution[];
  loading: boolean;
}

const EMPTY: AutomationExecution[] = [];
const STALE_MS = 5000;

const fetchExecutions = async (automationId: string) =>
  (await listExecutions(automationId, { limit: 20, offset: 0 })).data;

export function useExecutions(automationId: string): UseExecutionsResult {
  const { data, isLoading } = useQuery({
    queryKey: queryKeys.automations.executions(automationId),
    queryFn: () => fetchExecutions(automationId),
    refetchInterval: (query) => pollMs(!!query.state.data?.executions.some((e) => isRunLive(e.status))),
    refetchIntervalInBackground: false,
    staleTime: STALE_MS,
  });

  return { executions: data?.executions ?? EMPTY, loading: isLoading };
}

/** Warms an automation's runs before it is opened, so its pane renders whole
 *  instead of swapping a skeleton for the table a beat after arriving. */
export function usePrefetchExecutions() {
  const queryClient = useQueryClient();
  return useCallback(
    (automationId: string) => {
      void queryClient.prefetchQuery({
        queryKey: queryKeys.automations.executions(automationId),
        queryFn: () => fetchExecutions(automationId),
        staleTime: STALE_MS,
      });
    },
    [queryClient],
  );
}

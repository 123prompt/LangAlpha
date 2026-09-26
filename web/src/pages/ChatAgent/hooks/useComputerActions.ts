/**
 * The machine-level mutations: start and stop, rename, change spec, always-on.
 *
 * These used to hang off a workspace's menu, but tier and always-on are the
 * computer's, and every workspace on it changes with them. They are addressed
 * by computer id here, and every success refreshes both the machine rows and
 * the workspace rows, which repeat the machine's values.
 */
import { useCallback } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { toast } from '@/components/ui/use-toast';
import { queryKeys } from '@/lib/queryKeys';
import type { ComputersResponse, ResourceTier } from '@/types/api';

import {
  renameComputer,
  setComputerAlwaysOn,
  setComputerSpec,
  startComputer,
  stopComputer,
} from '../utils/api';
import { entitlementErrorMessage } from '../utils/entitlementErrors';
import { tierLabel } from '../components/tierUi';
import { closeComputerSpec } from './computerPanelStore';
import {
  invalidateMachine,
  invalidateMachineStorage,
  patchComputerRow,
  patchComputerStatusInCaches,
} from './useComputers';

export type PowerAction = 'start' | 'stop';

export function useComputerActions() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  // Start is lazy: the 202 says 'starting' and the status stream reports the rest.
  const power = useMutation({
    mutationFn: ({ computerId, action }: { computerId: string; action: PowerAction }) =>
      action === 'start' ? startComputer(computerId, { lazy: true }) : stopComputer(computerId),
    onSuccess: (res) => {
      // Reflecting the response status is what brings a transitional machine
      // into the fan-out's watch set, so the stream reports the rest.
      patchComputerStatusInCaches(queryClient, res.computer_id, res.status);
      void queryClient.invalidateQueries({ queryKey: queryKeys.workspaces.all });
      // The breakdown read before this action describes the old power state.
      invalidateMachineStorage(queryClient, res.computer_id);
    },
  });

  const rename = useMutation({
    mutationFn: ({ computerId, name }: { computerId: string; name: string }) => renameComputer(computerId, name),
    onMutate: ({ computerId, name }) => {
      const snapshot = queryClient.getQueriesData<ComputersResponse | undefined>({ queryKey: queryKeys.computers.lists() });
      patchComputerRow(queryClient, computerId, { name });
      return { snapshot };
    },
    onError: (err, _vars, ctx) => {
      for (const [key, data] of ctx?.snapshot ?? []) queryClient.setQueryData(key, data);
      toast({
        variant: 'destructive',
        title: t('computer.renameFailed', 'Could not rename computer'),
        description: entitlementErrorMessage(err, t),
      });
    },
    onSettled: () => invalidateMachine(queryClient),
  });

  // Settles as soon as the server accepts the change. The change itself runs
  // afterwards; the list polls while it does, and ComputersDialogHost reports
  // how it went.
  const spec = useMutation({
    mutationFn: ({ computerId, tier }: { computerId: string; tier: ResourceTier }) => setComputerSpec(computerId, tier),
    onSuccess: async (row, { computerId, tier }) => {
      // A list read that left before the request would land after this patch
      // and put the row back without its change, which also stops the poll.
      await queryClient.cancelQueries({ queryKey: queryKeys.computers.lists() });
      patchComputerRow(queryClient, computerId, row);
      if (row.spec_change?.state !== 'in_progress') {
        // Already at that tier: nothing was scheduled.
        toast({ title: t('workspace.specUpdated', 'Computer spec updated'), description: tierLabel(t, tier) });
        invalidateMachine(queryClient);
        closeComputerSpec();
      }
    },
    onError: () => invalidateMachine(queryClient),
  });

  const alwaysOn = useMutation({
    mutationFn: ({ computerId, enabled }: { computerId: string; enabled: boolean }) =>
      setComputerAlwaysOn(computerId, enabled),
    onMutate: ({ computerId, enabled }) => {
      const snapshot = queryClient.getQueriesData<ComputersResponse | undefined>({ queryKey: queryKeys.computers.lists() });
      patchComputerRow(queryClient, computerId, { is_always_on: enabled });
      return { snapshot };
    },
    onError: (err, _vars, ctx) => {
      for (const [key, data] of ctx?.snapshot ?? []) queryClient.setQueryData(key, data);
      toast({
        variant: 'destructive',
        title: t('workspace.alwaysOnFailed'),
        description: entitlementErrorMessage(err, t),
      });
    },
    onSettled: () => invalidateMachine(queryClient),
  });

  const renameComputerTo = useCallback(
    (computerId: string, name: string) => rename.mutate({ computerId, name }),
    [rename],
  );

  return { power, rename, renameComputerTo, spec, alwaysOn };
}

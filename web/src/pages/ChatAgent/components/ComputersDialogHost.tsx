/**
 * Mounts the computer dialogs once, at the page root, driven by
 * `computerPanelStore`. The gallery, a card's machine line and the disk
 * warnings in a chat all open them, and none of those share a parent below
 * the root.
 *
 * Being always mounted and reading the list, it is also where list-wide
 * bookkeeping lives: announcing spec-change outcomes and forgetting disk
 * warning dismissals once a disk recovers.
 */
import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQueryClient } from '@tanstack/react-query';

import { toast } from '@/components/ui/use-toast';
import type { Computer, ResourceTier } from '@/types/api';

import {
  closeComputerSpec,
  closeComputersPanel,
  useComputersPanel,
} from '../hooks/computerPanelStore';
import { useComputerActions } from '../hooks/useComputerActions';
import { invalidateMachine, invalidateMachineStorage, useComputers } from '../hooks/useComputers';
import { useTierQuota } from '../hooks/useTierQuota';
import ChangeSpecDialog from './ChangeSpecDialog';
import ComputersDialog from './ComputersDialog';
import { useForgetRecoveredDismissals } from './DiskWarning';
import { activeSpecChange } from './specChangeUi';
import { specErrorFiles, specErrorFrom, specErrorFromOutcome, type SpecError } from './specErrors';
import { normalizeTier, tierLabel } from './tierUi';

function ComputersDialogHost() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { panel, specComputerId } = useComputersPanel();
  // Always on: the fan-out at the same root reads this list anyway, and the
  // outcome watch below needs every refetch, whoever triggered it.
  const { data } = useComputers();
  const computers = data?.computers;
  const specTarget = specComputerId
    ? computers?.find((c) => c.computer_id === specComputerId) ?? null
    : null;
  const { spec } = useComputerActions();
  const [specError, setSpecError] = useState<SpecError | null>(null);
  const { data: quota } = useTierQuota({ enabled: !!specTarget });

  useForgetRecoveredDismissals(computers);

  useEffect(() => {
    setSpecError(null);
  }, [specComputerId]);

  // Changes this tab has seen in progress, by computer, with the change's
  // start so a stale row that predates it is not read as its outcome.
  const inProgressRef = useRef<Map<string, string>>(new Map());

  // Any list read can be the one that sees a change settle (the poll, window
  // focus, a status reconcile, an invalidation), so the outcome is read off
  // every new list rather than off a poll of our own. Only changes seen in
  // progress are announced; an outcome that settled before load stays quiet.
  useEffect(() => {
    if (!computers) return;
    const watched = inProgressRef.current;
    const next = new Map<string, string>();
    const settled: Computer[] = [];
    for (const c of computers) {
      const running = activeSpecChange(c);
      const startedAt = watched.get(c.computer_id);
      if (running) {
        next.set(c.computer_id, running.started_at);
      } else if (startedAt !== undefined) {
        if (c.spec_change && c.spec_change.started_at === startedAt) settled.push(c);
        else next.set(c.computer_id, startedAt);
      }
    }
    inProgressRef.current = next;

    // The dialog may be closed long before the change settles, so the outcome
    // is reported wherever the user is: inline when the dialog is still open
    // on that computer, as a toast otherwise.
    for (const computer of settled) {
      const change = computer.spec_change;
      if (!change) continue;
      // A new tier is a new disk, so the breakdown is re-read here and
      // nowhere else on this path.
      invalidateMachine(queryClient);
      invalidateMachineStorage(queryClient, computer.computer_id);
      const dialogOpen = specComputerId === computer.computer_id;
      if (change.state === 'succeeded') {
        toast({
          title: t('workspace.specUpdated', 'Computer spec updated'),
          description: t('computer.spec.succeeded', '{{name}} now runs at {{tier}}.', {
            name: computer.name,
            tier: tierLabel(t, normalizeTier(change.target_tier)),
          }),
        });
        if (dialogOpen) closeComputerSpec();
        continue;
      }
      const error = specErrorFromOutcome(change, t);
      if (dialogOpen) {
        setSpecError(error);
        continue;
      }
      const files = specErrorFiles(error.files, t);
      toast({
        variant: 'destructive',
        title: t('computer.spec.failedTitle', 'Could not change the spec of {{name}}', { name: computer.name }),
        description: files.length > 0 ? `${error.message} ${files.join(', ')}` : error.message,
      });
    }
    // Re-running on a dialog or locale change is harmless: settled ids have
    // already left the watch set.
  }, [computers, specComputerId, queryClient, t]);

  const submitSpec = (tier: ResourceTier) => {
    if (!specComputerId) return;
    setSpecError(null);
    spec.mutate(
      { computerId: specComputerId, tier },
      { onError: (err) => setSpecError(specErrorFrom(err, t, tier)) },
    );
  };

  return (
    <>
      <ComputersDialog
        open={!!panel}
        onOpenChange={(open) => { if (!open) closeComputersPanel(); }}
        focusComputerId={panel?.computerId ?? null}
        expandStorage={panel?.expandStorage ?? false}
      />
      <ChangeSpecDialog
        target={specTarget}
        onClose={closeComputerSpec}
        onSubmit={submitSpec}
        busy={spec.isPending}
        quota={quota}
        error={specError}
      />
    </>
  );
}

export default ComputersDialogHost;

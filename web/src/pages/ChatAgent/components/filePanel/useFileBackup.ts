import { useCallback, useEffect, useRef, useState } from 'react';
import { backupWorkspaceFiles, getBackupStatus } from '../../utils/api';
import type { BackupResult } from './types';

/** COS backup status + manual backup trigger (skipped entirely in readOnly). */
export function useFileBackup({ workspaceId, files, readOnly }: {
  workspaceId: string;
  files: string[];
  readOnly?: boolean;
}) {
  // Backup state
  const [backedUpSet, setBackedUpSet] = useState<Set<string>>(new Set());
  const [modifiedSet, setModifiedSet] = useState<Set<string>>(new Set());
  const [backingUp, setBackingUp] = useState(false);
  const [backupResult, setBackupResult] = useState<BackupResult | null>(null);
  // An earlier result's dismissal must not clear a later one, which may be
  // a list of unsaved files meant to stay until the reader dismisses it.
  const dismissTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const dismissAfter = useCallback((ms: number) => {
    dismissTimer.current = setTimeout(() => setBackupResult(null), ms);
  }, []);
  useEffect(() => () => clearTimeout(dismissTimer.current), []);

  const updateBackupStatus = useCallback((data: { backed_up?: string[]; modified?: string[] }) => {
    setBackedUpSet(new Set(data.backed_up || []));
    setModifiedSet(new Set(data.modified || []));
  }, []);

  // Fetch backup status on mount and when files change (skip in readOnly mode)
  useEffect(() => {
    if (!workspaceId || readOnly) return;
    getBackupStatus(workspaceId)
      .then(updateBackupStatus)
      .catch(() => {});
  }, [workspaceId, files, updateBackupStatus, readOnly]);

  const handleBackup = useCallback(async () => {
    if (!workspaceId || backingUp) return;
    setBackingUp(true);
    clearTimeout(dismissTimer.current);
    setBackupResult(null);
    try {
      const result = await backupWorkspaceFiles(workspaceId);
      setBackupResult(result);
      const status = await getBackupStatus(workspaceId);
      updateBackupStatus(status);
      // A clean backup confirms and gets out of the way. One that skipped
      // files stays until dismissed: it is the only place the reader learns
      // which files are not safe.
      if (!result.unsaved_count && !result.unsaved?.length) {
        dismissAfter(3000);
      }
    } catch (err: unknown) {
      const e = err as { response?: { data?: { detail?: string } }; message?: string };
      const msg = e?.response?.data?.detail || e?.message || 'Backup failed';
      setBackupResult({ error: msg });
      dismissAfter(4000);
    } finally {
      setBackingUp(false);
    }
  }, [workspaceId, backingUp, updateBackupStatus, dismissAfter]);

  return {
    backedUpSet,
    modifiedSet,
    backingUp,
    backupResult,
    setBackupResult,
    handleBackup,
  };
}

export type FileBackup = ReturnType<typeof useFileBackup>;

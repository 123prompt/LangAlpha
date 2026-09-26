import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Loader } from '@/components/ui/loader';
import { ErrorLink } from '@/components/ui/error-banner';
import { PLAN_URL, SUPPORT_EMAIL } from '@/config/supportLinks';
import type { Computer, ResourceTier, WorkspaceQuota } from '@/types/api';
import { TIER_ORDER, TierRadioGroup, isTierBlocked, normalizeTier, tierCapacity, tierLabel } from './tierUi';
import { DiskBar, diskUsageLabel, isDiskAlertLevel, nextTier } from './computerDiskUi';
import { specErrorFiles, type SpecError } from './specErrors';
import { activeSpecChange, effectiveTier, specChangeLabel } from './specChangeUi';

interface ChangeSpecDialogProps {
  target: Pick<Computer, 'name' | 'resource_tier' | 'status' | 'disk' | 'spec_change'> | null;
  onClose: () => void;
  onSubmit: (tier: ResourceTier) => void;
  /** The request is in flight. It answers once the change is accepted, so this is brief. */
  busy: boolean;
  /** Per-tier count quotas (platform mode only); null/undefined hides the capacity hint. */
  quota?: WorkspaceQuota | null;
  /** The last refusal, shown inline so a list of files can be read and acted on. */
  error?: SpecError | null;
}

/**
 * Change a computer's resource tier. The machine is backed up and recreated,
 * which is slow and touches every workspace on it, so the dialog says so
 * before the click and keeps saying so while it runs. The change runs on the
 * server, so its progress comes from the row: closing the dialog stops
 * nothing, and reopening it shows the same change still going.
 */
function ChangeSpecDialog({ target, onClose, onSubmit, busy, quota, error }: ChangeSpecDialogProps) {
  const { t } = useTranslation();
  const [tier, setTier] = useState<ResourceTier>('standard');

  // Seeded per opening, and only until the user picks: the row behind
  // `target` is replaced on every list refetch, which must not throw away
  // that pick. Opened from a disk warning, the useful default is one step up,
  // but only where the plan allows it, and the quota that says so lands after
  // the dialog opens, so the seed is re-evaluated as it arrives.
  const targetRef = useRef(target);
  targetRef.current = target;
  const pickedRef = useRef(false);
  const isOpen = !!target;
  useEffect(() => {
    if (isOpen) pickedRef.current = false;
  }, [isOpen]);
  useEffect(() => {
    const opened = targetRef.current;
    if (!isOpen || !opened || pickedRef.current) return;
    const current = effectiveTier(opened);
    const up = isDiskAlertLevel(opened.disk?.level) ? nextTier(current) : null;
    setTier(up && !isTierBlocked(tierCapacity(quota, up)) ? up : current);
  }, [isOpen, quota]);
  const pickTier = (next: ResourceTier) => {
    pickedRef.current = true;
    setTier(next);
  };

  const currentTier = effectiveTier(target);
  const isRunning = target?.status === 'running';
  // The radio for a blocked tier is disabled, but a seed or a quota refetch
  // can still land the selection on one; the request would only earn a 403.
  const tierBlocked = tier !== currentTier && isTierBlocked(tierCapacity(quota, tier));
  const errorFiles = specErrorFiles(error?.files, t);
  const change = activeSpecChange(target);
  // A bigger tier the plan keeps closed can't be picked here, so the dialog
  // points to where that is decided instead of leaving a dead end.
  const planLimited =
    !change &&
    (PLAN_URL !== null || SUPPORT_EMAIL !== null) &&
    TIER_ORDER.slice(TIER_ORDER.indexOf(currentTier) + 1).some((id) => isTierBlocked(tierCapacity(quota, id)));
  // The row is the only witness to where the change is. It reads 'starting'
  // from before the backup until the new sandbox is up, so that one phase
  // covers both; 'running' is the moment before the machine is claimed.
  const phase = !change
    ? null
    : isRunning
      ? t('computer.spec.phaseBackup', 'Backing up every workspace on this computer.')
      : target?.status === 'starting' || target?.status === 'creating'
        ? t('computer.spec.phaseRebuild', 'Backing up your files and applying the {{tier}} spec.', {
            tier: tierLabel(t, normalizeTier(change.target_tier)),
          })
        : t('computer.spec.phaseApply', 'Applying the new spec.');

  return (
    <Dialog open={!!target} onOpenChange={(open) => { if (!open && !busy) onClose(); }}>
      <DialogContent style={{ backgroundColor: 'var(--color-bg-page)', borderColor: 'var(--color-border-muted)' }}>
        <DialogHeader>
          <DialogTitle>
            {t('computer.spec.title', 'Change spec for {{name}}', { name: target?.name ?? '' })}
          </DialogTitle>
          <DialogDescription>
            {isRunning
              ? t(
                  'computer.spec.descRunning',
                  'Every workspace on this computer is backed up first. The computer is then recreated at the new spec and your files are restored. Workspaces on it are unavailable for a few minutes.',
                )
              : t(
                  'computer.spec.descStopped',
                  'Every workspace on this computer is backed up first. The new spec applies the next time the computer starts, and your files are restored then.',
                )}{' '}
            {t(
              'computer.spec.notKept',
              "Installed packages, caches and running apps aren't kept. The Agent reinstalls what it needs.",
            )}
          </DialogDescription>
        </DialogHeader>
        {target?.disk && (
          <div className="flex flex-col gap-1.5">
            <DiskBar disk={target.disk} />
            <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              {diskUsageLabel(t, target.disk)}
            </span>
          </div>
        )}
        {change ? (
          <div
            className="flex items-start gap-2 rounded-md px-3 py-2.5 text-sm"
            role="status"
            aria-live="polite"
            data-testid="spec-change-progress"
            style={{ backgroundColor: 'var(--color-bg-card)', color: 'var(--color-text-primary)' }}
          >
            <Loader size={14} className="mt-0.5 text-[color:var(--color-accent-primary)]" />
            <div className="flex flex-col gap-0.5">
              <span className="font-medium">{specChangeLabel(t, change)}</span>
              <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                {phase}{' '}
                {t('computer.spec.progressHint', 'This can take a few minutes. You can close this dialog; the change keeps running.')}
              </span>
            </div>
          </div>
        ) : (
          <TierRadioGroup
            value={tier}
            onChange={pickTier}
            label={t('workspace.changeSpec', 'Change spec')}
            quota={quota}
            exemptTier={currentTier}
          />
        )}
        {planLimited && (
          <p className="text-xs" data-testid="spec-plan-cta" style={{ color: 'var(--color-text-secondary)' }}>
            {t('computer.spec.planLimited', 'Need a bigger spec than your plan includes?')}{' '}
            <span style={{ color: 'var(--color-text-primary)' }}>
              {PLAN_URL && <ErrorLink url={PLAN_URL} label={t('computer.spec.upgradePlan', 'Upgrade plan')} external />}
              {PLAN_URL && SUPPORT_EMAIL && <span aria-hidden="true"> · </span>}
              {SUPPORT_EMAIL && (
                <a href={`mailto:${SUPPORT_EMAIL}`} style={{ textDecoration: 'underline', fontWeight: 500 }}>
                  {t('computer.spec.contactSupport', 'Contact support')}
                </a>
              )}
            </span>
          </p>
        )}
        {error && !busy && !change && (
          <div
            role="alert"
            className="rounded-md px-3 py-2 text-sm"
            style={{ backgroundColor: 'var(--color-loss-soft)', color: 'var(--color-text-primary)' }}
          >
            <p>{error.message}</p>
            {errorFiles.length > 0 && (
              <ul className="mt-1.5 list-disc pl-5 text-xs font-mono break-all" style={{ color: 'var(--color-text-secondary)' }}>
                {errorFiles.map((line) => <li key={line}>{line}</li>)}
              </ul>
            )}
          </div>
        )}
        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            {change ? t('common.close', 'Close') : t('common.cancel')}
          </Button>
          {!change && (
            <Button onClick={() => onSubmit(tier)} disabled={busy || tier === currentTier || tierBlocked}>
              {busy ? t('computer.spec.applying', 'Applying') : t('computer.spec.apply', 'Back up and change spec')}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default ChangeSpecDialog;

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import type { Computer } from '@/types/api';

interface AlwaysOnConfirmDialogProps {
  target: Pick<Computer, 'name' | 'status'> | null;
  onClose: () => void;
  onConfirm: () => void;
  busy: boolean;
}

/**
 * Confirm enabling always-on (24/7 billing) for a computer. A stopped or
 * never-started machine starts immediately (the server's CLAIMABLE_FOR_START),
 * so the copy calls that out.
 */
function AlwaysOnConfirmDialog({ target, onClose, onConfirm, busy }: AlwaysOnConfirmDialogProps) {
  const { t } = useTranslation();
  const startsNow = target?.status === 'stopped' || target?.status === 'creating';

  return (
    <Dialog open={!!target} onOpenChange={(open) => { if (!open && !busy) onClose(); }}>
      <DialogContent style={{ backgroundColor: 'var(--color-bg-page)', borderColor: 'var(--color-border-muted)' }}>
        <DialogHeader>
          <DialogTitle>{t('workspace.alwaysOnEnable', 'Turn on always-on')}</DialogTitle>
          <DialogDescription>
            {startsNow
              ? t('computer.alwaysOnConfirmStopped', {
                  name: target?.name ?? '',
                  defaultValue: 'Start "{{name}}" and keep it running 24/7? Every workspace on it stays available. The computer starts immediately and keeps billing until always-on is turned off.',
                })
              : t('computer.alwaysOnConfirm', {
                  name: target?.name ?? '',
                  defaultValue: 'Keep "{{name}}" running 24/7? Every workspace on it stays available. The computer skips idle shutdown and keeps billing until always-on is turned off.',
                })}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </Button>
          <Button onClick={onConfirm} disabled={busy}>
            {busy ? t('common.saving') : t('workspace.alwaysOnEnableConfirm', 'Turn on')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default AlwaysOnConfirmDialog;

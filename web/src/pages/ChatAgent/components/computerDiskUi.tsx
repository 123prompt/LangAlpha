/**
 * Disk vocabulary for a computer: the level ladder, its colors, and the bar.
 *
 * The level is decided server-side from free bytes, so every surface here only
 * maps it to presentation; none of them re-derives it from the numbers.
 */
import { useTranslation } from 'react-i18next';

import { formatBytes } from '@/lib/format';
import { cn } from '@/lib/utils';
import type { ComputerDisk, ComputerDiskLevel, ResourceTier } from '@/types/api';

import { TIER_ORDER } from './tierUi';

type Translate = ReturnType<typeof useTranslation>['t'];

const LEVEL_RANK: Record<ComputerDiskLevel, number> = {
  healthy: 0,
  notice: 1,
  warning: 2,
  critical: 3,
};

export function diskLevelRank(level: ComputerDiskLevel | null | undefined): number {
  return level ? LEVEL_RANK[level] ?? 0 : 0;
}

/** Levels that earn a banner. Notice stays in the storage bar and the card dot. */
export function isDiskAlertLevel(level: ComputerDiskLevel | null | undefined): level is 'warning' | 'critical' {
  return level === 'warning' || level === 'critical';
}

/** Fill color of the bar and the card dot, by level. */
export function diskLevelColor(level: ComputerDiskLevel | null | undefined): string {
  switch (level) {
    case 'critical':
      return 'var(--color-loss)';
    case 'warning':
      return 'var(--color-warning)';
    // The accent annotates; it is not a state color, so notice stays neutral.
    case 'notice':
      return 'var(--color-text-secondary)';
    default:
      return 'var(--color-text-tertiary)';
  }
}

/** The tier one step up, or null on the top tier. */
export function nextTier(tier: ResourceTier): ResourceTier | null {
  const index = TIER_ORDER.indexOf(tier);
  return index >= 0 && index < TIER_ORDER.length - 1 ? TIER_ORDER[index + 1] : null;
}

export function diskUsageLabel(t: Translate, disk: ComputerDisk): string {
  return t('computer.disk.usage', '{{used}} of {{total}} used, {{free}} free', {
    used: formatBytes(disk.used_bytes),
    total: formatBytes(disk.total_bytes),
    free: formatBytes(disk.free_bytes),
  });
}

interface DiskBarProps {
  disk: ComputerDisk;
  className?: string;
}

/** Thin usage bar. The label beside it carries the numbers; the bar is the glance. */
export function DiskBar({ disk, className }: DiskBarProps) {
  const { t } = useTranslation();
  const ratio = disk.total_bytes > 0 ? Math.min(1, Math.max(0, disk.used_bytes / disk.total_bytes)) : 0;
  return (
    <div
      className={cn('h-1.5 w-full overflow-hidden rounded-full', className)}
      style={{ backgroundColor: 'var(--color-border-muted)' }}
      role="meter"
      aria-valuemin={0}
      aria-valuemax={disk.total_bytes}
      aria-valuenow={disk.used_bytes}
      aria-label={t('computer.disk.label', 'Storage')}
      aria-valuetext={diskUsageLabel(t, disk)}
    >
      <div
        className="h-full rounded-full transition-[width] duration-300"
        style={{ width: `${Math.round(ratio * 1000) / 10}%`, backgroundColor: diskLevelColor(disk.level) }}
      />
    </div>
  );
}

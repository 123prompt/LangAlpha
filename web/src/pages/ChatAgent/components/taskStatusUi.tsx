/**
 * The one place a task's status becomes an icon and a color.
 *
 * Two vocabularies live here because the surfaces genuinely speak two: inline
 * cards read `TaskCardStatusKind` (which carries the update/resume verbs a
 * card header shows), while the nav tree and the status bar read a subagent's
 * `SubagentDisplayStatus` (which distinguishes "spawned but silent" from
 * "running"). Each vocabulary has exactly one table; a surface that needs its
 * own treatment declares an override rather than restating the ladder.
 */
import React from 'react';
import { useTranslation } from 'react-i18next';
import {
  AlertCircle, Check, CheckCircle2, Circle, Loader2, RefreshCw, RotateCw, StopCircle,
  type LucideIcon,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import type { SubagentDisplayStatus } from '../session/subagents/subagentStatus';

export type TaskCardStatusKind =
  | 'running'
  | 'completed'
  | 'cancelled'
  | 'error'
  | 'updated'
  | 'resumed'
  | 'unknown';

interface TaskCardStatusUi {
  /** i18n key for the status word; `null` renders the raw wire status instead. */
  labelKey: string | null;
  color: string;
  Icon: LucideIcon | null;
  spin: boolean;
}

/**
 * Per-status presentation for every inline task card. Subagent spawns and
 * workflow runs read this one table, so a status can never render amber on one
 * card and red on the other. Updated/Resumed share the warning amber with
 * Running because those are all "in flight or just changed"; Cancelled is
 * terminal-neutral (the run was stopped), Failed is the loss token.
 */
export const STATUS_UI: Record<TaskCardStatusKind, TaskCardStatusUi> = {
  running: {
    labelKey: 'chat.taskCard.statusRunning',
    color: 'var(--color-warning)',
    Icon: Loader2,
    spin: true,
  },
  completed: {
    labelKey: 'chat.taskCard.statusCompleted',
    color: 'var(--color-success)',
    Icon: Check,
    spin: false,
  },
  cancelled: {
    labelKey: 'chat.taskCard.statusStopped',
    color: 'var(--color-text-tertiary)',
    Icon: StopCircle,
    spin: false,
  },
  error: {
    labelKey: 'chat.taskCard.statusFailed',
    color: 'var(--color-loss)',
    Icon: AlertCircle,
    spin: false,
  },
  updated: {
    labelKey: 'chat.taskCard.statusUpdated',
    color: 'var(--color-warning)',
    Icon: RefreshCw,
    spin: false,
  },
  resumed: {
    labelKey: 'chat.taskCard.statusResumed',
    color: 'var(--color-warning)',
    Icon: RotateCw,
    spin: false,
  },
  unknown: {
    labelKey: null,
    color: 'var(--color-text-tertiary)',
    Icon: null,
    spin: false,
  },
};

/**
 * The status word every task surface shows: icon, accent and label from one
 * `STATUS_UI` row. The inline card header and the workflow-run detail header
 * render this, so the chip cannot drift between them.
 */
export function TaskStatusChip({
  kind,
  rawStatus,
  style,
}: {
  kind: TaskCardStatusKind;
  /** Wire status rendered verbatim when `kind` is `unknown`. */
  rawStatus?: string;
  style?: React.CSSProperties;
}): React.ReactElement {
  const { t } = useTranslation();
  const { labelKey, color, Icon, spin } = STATUS_UI[kind];
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 5,
        color,
        fontSize: 11,
        letterSpacing: '0.04em',
        fontWeight: 500,
        whiteSpace: 'nowrap',
        ...style,
      }}
    >
      {Icon && (
        <Icon
          style={{
            width: 11,
            height: 11,
            animation: spin ? 'spin 1s linear infinite' : undefined,
          }}
        />
      )}
      {labelKey ? t(labelKey) : rawStatus}
    </span>
  );
}

interface SubagentStatusTreatment {
  Icon: LucideIcon;
  color: string;
  spin?: boolean;
}

/**
 * A subagent's status badge: terminal outcomes read at a glance (error = red
 * alert, stopped = stop glyph, done = check), a genuinely running task spins,
 * and a spawned-but-silent one holds an idle circle.
 */
const SUBAGENT_STATUS_UI: Record<SubagentDisplayStatus, SubagentStatusTreatment> = {
  initializing: { Icon: Circle, color: 'var(--color-icon-muted)' },
  active: { Icon: Loader2, color: 'var(--color-text-tertiary)', spin: true },
  completed: { Icon: Check, color: 'var(--color-text-tertiary)' },
  cancelled: { Icon: StopCircle, color: 'var(--color-text-tertiary)' },
  error: { Icon: AlertCircle, color: 'var(--color-loss)' },
};

export type SubagentStatusSurface = 'navRow' | 'statusBar';

/**
 * The status bar is the one surface that celebrates a finished task — it has
 * the room the nav row doesn't. Whether the two should converge is a design
 * call, not a structural one; until it's made, the difference is declared in
 * one place instead of implied by two ladders.
 */
const SUBAGENT_STATUS_OVERRIDES: Partial<
  Record<SubagentStatusSurface, Partial<Record<SubagentDisplayStatus, SubagentStatusTreatment>>>
> = {
  statusBar: {
    completed: { Icon: CheckCircle2, color: 'var(--color-accent-primary)' },
  },
};

export function SubagentStatusIcon({
  status,
  surface = 'navRow',
  className,
}: {
  status: SubagentDisplayStatus;
  surface?: SubagentStatusSurface;
  /** Sizing class — the nav row is 3, the status bar 4. */
  className?: string;
}): React.ReactElement {
  const { Icon, color, spin } =
    SUBAGENT_STATUS_OVERRIDES[surface]?.[status] ?? SUBAGENT_STATUS_UI[status];
  return <Icon className={cn(className, spin && 'animate-spin')} style={{ color }} />;
}

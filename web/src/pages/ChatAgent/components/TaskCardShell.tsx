import React from 'react';
import { ChevronRight } from 'lucide-react';
import { TaskStatusChip, type TaskCardStatusKind } from './taskStatusUi';

export const MONO_STACK = 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
export const INTER_STACK =
  "'Inter', system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";

interface TaskCardShellProps {
  /** Lowercase type token on the left of the header rule. */
  eyebrow: React.ReactNode;
  statusKind: TaskCardStatusKind;
  /** Wire status rendered verbatim when `statusKind` is `unknown`. */
  rawStatus?: string;
  /** Trailing header affordance; defaults to the muted chevron. */
  affordance?: React.ReactNode;
  title: string;
  /** Card tooltip. */
  hint: string;
  onOpen: () => void;
  testId?: string;
  /** Body content below the title. */
  children?: React.ReactNode;
  /** Content for the hairline band under the body. */
  footer?: React.ReactNode;
}

/**
 * The chrome every inline task card shares: clickable/focusable container,
 * the type · status · affordance header rule, the title stack, and the
 * optional footer band. Callers supply only what differs — the eyebrow, the
 * status kind, and the body.
 */
export function TaskCardShell({
  eyebrow,
  statusKind,
  rawStatus,
  affordance,
  title,
  hint,
  onOpen,
  testId,
  children,
  footer,
}: TaskCardShellProps): React.ReactElement {
  const handleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>): void => {
    // Ignore keystrokes that originated on a descendant control (e.g. the
    // "View subagent output" button) — the descendant handles its own
    // activation, and the keydown shouldn't double-fire as a card click.
    if (e.target !== e.currentTarget) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onOpen();
    }
  };

  return (
    <div
      role="button"
      tabIndex={0}
      data-testid={testId}
      style={{
        background: 'var(--color-bg-tool-card)',
        border: '1px solid var(--color-border-muted)',
        borderRadius: 12,
        overflow: 'hidden',
        cursor: 'pointer',
        fontFamily: MONO_STACK,
        transition: 'border-color 0.15s',
      }}
      onClick={onOpen}
      onKeyDown={handleKeyDown}
      onMouseEnter={(e: React.MouseEvent<HTMLDivElement>) => (e.currentTarget.style.borderColor = 'var(--color-border-default)')}
      onMouseLeave={(e: React.MouseEvent<HTMLDivElement>) => (e.currentTarget.style.borderColor = 'var(--color-border-muted)')}
      title={hint}
    >
      {/* Rule: type · status · affordance */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          padding: '10px 12px 8px 14px',
          borderBottom: '1px solid var(--color-border-subtle)',
          fontSize: 12,
        }}
      >
        <span
          style={{
            color: 'var(--color-text-secondary)',
            fontWeight: 500,
            textTransform: 'lowercase',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
            minWidth: 0,
            flex: '0 1 auto',
          }}
        >
          {eyebrow}
        </span>
        <span style={{ flex: 1 }} />
        <TaskStatusChip kind={statusKind} rawStatus={rawStatus} />
        {affordance ?? (
          <ChevronRight
            aria-hidden="true"
            style={{ width: 14, height: 14, flexShrink: 0, color: 'var(--color-text-quaternary)' }}
          />
        )}
      </div>

      <div style={{ padding: '12px 14px 14px' }}>
        <div
          style={{
            fontFamily: INTER_STACK,
            fontSize: 14,
            fontWeight: 500,
            color: 'var(--color-text-primary)',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {title}
        </div>
        {children}
      </div>

      {footer && (
        <div
          style={{
            padding: '7px 14px',
            borderTop: '1px solid var(--color-border-subtle)',
            fontSize: 11,
            color: 'var(--color-text-tertiary)',
            letterSpacing: '0.02em',
          }}
        >
          {footer}
        </div>
      )}
    </div>
  );
}

export default TaskCardShell;

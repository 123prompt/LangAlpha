import React from 'react';

function renderValue(value: unknown): string {
  if (value == null) return String(value);
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

/**
 * The arguments of a direct tool call as a key/value list. Nothing is masked
 * here: this is where the user reads exactly what is, or was, sent.
 */
export function ArgsTable({ args, emptyLabel }: { args: Record<string, unknown>; emptyLabel: string }): React.ReactElement {
  const entries = Object.entries(args).filter(([, v]) => v !== undefined);
  if (entries.length === 0) {
    return <div className="text-xs" style={{ color: 'var(--color-text-quaternary)' }}>{emptyLabel}</div>;
  }
  return (
    <dl className="grid gap-x-4 gap-y-1 text-xs" style={{ gridTemplateColumns: 'max-content minmax(0, 1fr)' }}>
      {entries.map(([k, v]) => (
        <React.Fragment key={k}>
          <dt className="font-mono whitespace-nowrap" style={{ color: 'var(--color-text-tertiary)' }}>{k}</dt>
          <dd className="font-mono break-all min-w-0" style={{ color: 'var(--color-text-primary)' }}>{renderValue(v)}</dd>
        </React.Fragment>
      ))}
    </dl>
  );
}

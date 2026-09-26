import React from 'react';
import { useTranslation } from 'react-i18next';
import { Clock } from 'lucide-react';
import { useAutomationMutations } from '@/pages/Automations/hooks/useAutomationMutations';
import { useWaitingAutomations } from '@/pages/Automations/hooks/useWaitingAutomations';

/**
 * Automations that came due on this thread while a turn runs here. They wait
 * for the turn to end rather than steering into it; the run each one starts
 * reaches this view like any run started elsewhere (ChatView's feed effect).
 */
export function QueuedAutomationNotice({ threadId, active }: { threadId: string; active: boolean }) {
  const { t } = useTranslation();
  const waiting = useWaitingAutomations(threadId, active);
  const { skip, busy } = useAutomationMutations();

  // Absent rather than empty: the composer column spaces every child it holds.
  if (!waiting.length) return null;
  return (
    <div className="space-y-3" role="status" aria-live="polite">
      {waiting.map((run) => {
        // Truncated beside the Skip button, so the whole line is its title.
        const line = t('chat.automationWaiting', { name: run.automation_name });
        return (
          <div
            key={run.automation_execution_id}
            className="flex items-center gap-2 px-3 py-1.5 text-xs"
            style={{ color: 'var(--color-text-tertiary)' }}
          >
            <Clock aria-hidden="true" className="h-3.5 w-3.5 flex-shrink-0" style={{ color: 'var(--color-accent-primary)' }} />
            <span className="min-w-0 truncate" title={line}>
              {line}
            </span>
            <button
              type="button"
              className="ml-auto flex-shrink-0 text-xs underline-offset-2 hover:underline"
              style={{ color: 'var(--color-text-secondary)' }}
              disabled={busy}
              onClick={() => skip.mutate({ automationId: run.automation_id, executionId: run.automation_execution_id })}
            >
              {t('chat.automationWaitingSkip')}
            </button>
          </div>
        );
      })}
    </div>
  );
}

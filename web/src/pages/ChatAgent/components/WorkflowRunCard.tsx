import React from 'react';
import { useTranslation } from 'react-i18next';
import { compactNumber } from '@/lib/format';
import {
  WORKFLOW_TASK_TYPE,
  workflowRunCardKind,
  workflowRunDisplayStatus,
  type WorkflowRunState,
} from '../session/subagents/workflowRunState';
import { useWorkflowRun } from './WorkflowRunContext';
import TaskCardShell from './TaskCardShell';
import { WorkflowChildRow, summarizeRun } from './workflowRunUi';

/** Most recent child rows shown inline; older settled rows collapse to a count. */
const MAX_VISIBLE_CHILDREN = 8;

interface WorkflowSubagentInfo {
  subagentId: string;
  description: string;
  type: string;
  status: string;
}

interface WorkflowRunCardProps {
  subagentId?: string;
  description?: string;
  /** Inline-record status ('running' | 'completed' | 'cancelled' | 'error') —
   *  the fallback before any workflow_lifecycle state has arrived. */
  status?: string;
  onOpen?: (info: WorkflowSubagentInfo) => void;
  /** Direct state override for tests; the context resolver is the live path. */
  workflowRun?: WorkflowRunState;
}

/**
 * Inline workflow-run card: a quiet operations console for one RunWorkflow
 * launch. Header rule names the workflow and its status; the body shows the
 * phase rail, the live child fan-out (rows appear on dispatch and settle in
 * place), and the latest log line; a hairline meta band carries the totals —
 * flipping to a flat mission-report row once the run settles.
 */
function WorkflowRunCard({
  subagentId,
  description,
  status = 'running',
  onOpen,
  workflowRun: workflowRunProp,
}: WorkflowRunCardProps): React.ReactElement | null {
  const { t } = useTranslation();
  const ctxRun = useWorkflowRun(subagentId);
  const run = workflowRunProp ?? ctxRun;

  if (!subagentId && !description && !run) return null;

  const kind = workflowRunCardKind(run?.status ?? status);
  const isRunning = kind === 'running';

  const { children, doneCount, agentCount, duration: totalDuration } = summarizeRun(run);
  const hiddenCount = Math.max(0, children.length - MAX_VISIBLE_CHILDREN);
  const visibleChildren = hiddenCount > 0 ? children.slice(-MAX_VISIBLE_CHILDREN) : children;
  const latestLog = isRunning && run?.logs.length ? run.logs[run.logs.length - 1] : null;

  const title = run?.description || description || run?.name || t('chat.workflowRun.titleFallback');

  const metaParts: string[] = [];
  if (agentCount > 0) {
    const agents = t('chat.workflowRun.agents', { count: agentCount });
    metaParts.push(
      isRunning && children.length > 0
        ? `${agents} · ${t('chat.workflowRun.done', { count: doneCount })}`
        : agents
    );
  }
  if (!isRunning && totalDuration) metaParts.push(totalDuration);
  if (run?.tokensSpent != null) {
    metaParts.push(t('chat.workflowRun.tokens', { value: compactNumber(run.tokensSpent) }));
  }

  const handleOpen = (): void => {
    onOpen?.({
      subagentId: subagentId || '',
      description: title,
      type: WORKFLOW_TASK_TYPE,
      // Run vocabulary ('failed') is not the card vocabulary — pass it through
      // unmapped and the opened card reads as unsettled. Mirrors the child rows
      // in WorkflowRunDetail, which normalize the same way.
      status: run ? workflowRunDisplayStatus(run.status) : status,
    });
  };

  return (
    <TaskCardShell
      testId="workflow-run-card"
      eyebrow={`${WORKFLOW_TASK_TYPE}${run?.name ? ` · ${run.name}` : ''}`}
      statusKind={kind}
      title={title}
      hint={t(isRunning ? 'chat.workflowRun.openRunning' : 'chat.workflowRun.openDetails')}
      onOpen={handleOpen}
      footer={metaParts.length > 0 ? <span data-testid="workflow-meta-band">{metaParts.join(' · ')}</span> : null}
    >
      {(run?.phases.length ?? 0) > 0 && (
        <div
          data-testid="workflow-phase-rail"
          style={{
            display: 'flex',
            alignItems: 'center',
            flexWrap: 'wrap',
            columnGap: 8,
            rowGap: 2,
            marginTop: 8,
            fontSize: 11,
            letterSpacing: '0.02em',
          }}
        >
          {run!.phases.map((phase, i) => {
            const isCurrent = isRunning && phase === run!.currentPhase;
            const isPast = !isCurrent && (!isRunning || run!.phases.indexOf(run!.currentPhase || '') > i);
            return (
              <React.Fragment key={phase}>
                {i > 0 && (
                  <span aria-hidden="true" style={{ color: 'var(--color-text-quaternary)' }}>
                    ─
                  </span>
                )}
                <span
                  style={{
                    color: isCurrent
                      ? 'var(--color-text-primary)'
                      : isPast
                        ? 'var(--color-text-tertiary)'
                        : 'var(--color-text-quaternary)',
                    fontWeight: isCurrent ? 600 : 500,
                  }}
                >
                  {phase}
                </span>
              </React.Fragment>
            );
          })}
        </div>
      )}

      {children.length > 0 && (
        <div style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 4 }}>
          {hiddenCount > 0 && (
            <div style={{ fontSize: 11, color: 'var(--color-text-quaternary)' }}>
              {t('chat.workflowRun.earlier', { count: hiddenCount })}
            </div>
          )}
          {visibleChildren.map((child) => (
            <WorkflowChildRow key={child.seq} child={child} surface="card" />
          ))}
        </div>
      )}

      {latestLog && (
        <div
          data-testid="workflow-latest-log"
          style={{
            marginTop: 8,
            fontSize: 11,
            color: 'var(--color-text-tertiary)',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          › {latestLog}
        </div>
      )}

      {kind === 'error' && run?.error && (
        <div
          style={{
            marginTop: 8,
            fontSize: 11,
            color: 'var(--color-loss)',
            display: '-webkit-box',
            WebkitLineClamp: 2,
            WebkitBoxOrient: 'vertical',
            overflow: 'hidden',
          }}
        >
          {run.error}
        </div>
      )}
    </TaskCardShell>
  );
}

export default WorkflowRunCard;

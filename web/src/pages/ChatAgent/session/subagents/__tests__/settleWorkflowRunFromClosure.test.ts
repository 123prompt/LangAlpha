import { describe, it, expect, vi } from 'vitest';
import { settleWorkflowRunFromClosure } from '../liveEventHandlers';
import {
  applyWorkflowLifecycle,
  workflowRunStatusFromLedger,
  type WorkflowRunState,
} from '../workflowRunState';
import type { TaskRefs } from '../../streamRefs';

const TASK = 'task:wf1';

/** A run mid-flight: started, one phase, two children still running. */
const runningRun = (): WorkflowRunState =>
  [
    { phase: 'run_started', name: 'briefs', description: 'Fan out' },
    { phase: 'phase', title: 'Research' },
    { phase: 'child_started', seq: 0, label: 'AAPL', child_task_id: 'c0' },
    { phase: 'child_started', seq: 1, label: 'NVDA', child_task_id: 'c1' },
  ].reduce<WorkflowRunState | undefined>(
    (state, evt) => applyWorkflowLifecycle(state, evt),
    undefined,
  )!;

const refsWith = (run: WorkflowRunState | undefined): Record<string, TaskRefs> => ({
  [TASK]: {
    contentOrderCounterRef: { current: 0 },
    currentReasoningIdRef: { current: null },
    currentToolCallIdRef: { current: null },
    messages: [],
    runIndex: 0,
    ...(run ? { workflowRun: run } : {}),
  } as TaskRefs,
});

describe('workflowRunStatusFromLedger', () => {
  // Must stay in lockstep with the projector's _mapped_run_status, or a
  // reloading viewer sees the card change its verdict.
  it.each([
    ['completed', 'completed'],
    ['cancelled', 'cancelled'],
    ['error', 'failed'],
    ['interrupted', 'failed'],
  ])('maps ledger %s to %s', (ledger, expected) => {
    expect(workflowRunStatusFromLedger(ledger)).toBe(expected);
  });

  it('declines to reconcile an absent or still-open row', () => {
    expect(workflowRunStatusFromLedger(null)).toBeNull();
    expect(workflowRunStatusFromLedger(undefined)).toBeNull();
    expect(workflowRunStatusFromLedger('in_progress')).toBeNull();
  });
});

describe('settleWorkflowRunFromClosure', () => {
  it('settles a running run from the ledger outcome when its worker died', () => {
    const subagentStateRefs = refsWith(runningRun());
    const updateSubagentCard = vi.fn();

    const handled = settleWorkflowRunFromClosure({
      taskId: TASK,
      outcome: 'error',
      subagentStateRefs,
      updateSubagentCard,
    });

    expect(handled).toBe(true);
    // The card reads workflowRun.status ahead of its own status, so the run
    // state — not just the card stamp — has to reach terminal.
    expect(subagentStateRefs[TASK].workflowRun?.status).toBe('failed');
    expect(updateSubagentCard).toHaveBeenCalledWith(
      TASK,
      expect.objectContaining({ status: 'error', isActive: false }),
    );
  });

  it('clears the spinner on children the dead driver never settled', () => {
    const subagentStateRefs = refsWith(runningRun());
    settleWorkflowRunFromClosure({
      taskId: TASK,
      outcome: 'error',
      subagentStateRefs,
      updateSubagentCard: vi.fn(),
    });
    expect(
      subagentStateRefs[TASK].workflowRun?.children.map((c) => c.status),
    ).toEqual(['cancelled', 'cancelled']);
  });

  it('never overwrites a run that got its own terminal frame', () => {
    // The driver's frame carries result/totals the ledger outcome lacks, and
    // a late closure must not downgrade a genuine success to a failure.
    const completed = applyWorkflowLifecycle(runningRun(), {
      phase: 'run_completed',
      status: 'completed',
      result_preview: '{"ok":true}',
    });
    const subagentStateRefs = refsWith(completed);
    const updateSubagentCard = vi.fn();

    const handled = settleWorkflowRunFromClosure({
      taskId: TASK,
      outcome: 'error',
      subagentStateRefs,
      updateSubagentCard,
    });

    expect(handled).toBe(false);
    expect(subagentStateRefs[TASK].workflowRun?.status).toBe('completed');
    expect(subagentStateRefs[TASK].workflowRun?.resultPreview).toBe('{"ok":true}');
    expect(updateSubagentCard).not.toHaveBeenCalled();
  });

  it('leaves a plain subagent task alone', () => {
    const subagentStateRefs = refsWith(undefined);
    const updateSubagentCard = vi.fn();
    expect(
      settleWorkflowRunFromClosure({
        taskId: TASK,
        outcome: 'error',
        subagentStateRefs,
        updateSubagentCard,
      }),
    ).toBe(false);
    expect(updateSubagentCard).not.toHaveBeenCalled();
  });

  it('does nothing without a terminal ledger outcome', () => {
    const subagentStateRefs = refsWith(runningRun());
    expect(
      settleWorkflowRunFromClosure({
        taskId: TASK,
        outcome: null,
        subagentStateRefs,
        updateSubagentCard: vi.fn(),
      }),
    ).toBe(false);
    expect(subagentStateRefs[TASK].workflowRun?.status).toBe('running');
  });
});

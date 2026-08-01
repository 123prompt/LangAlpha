/**
 * The detail panel prints a child's error directly beneath that child's row.
 * The two have to agree about how alarming the outcome is: a schema miss means
 * the child ran and only its output disagreed with the schema, which the row
 * says in amber. A hardcoded loss colour on the error made the same child read
 * calm and alarming at once, one line apart.
 */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import WorkflowRunDetail from '../WorkflowRunDetail';
import { WorkflowRunContext } from '../WorkflowRunContext';
import { createWorkflowRunState } from '../../session/subagents/workflowRunState';
import type {
  WorkflowChild,
  WorkflowChildStatus,
  WorkflowRunState,
} from '../../session/subagents/workflowRunState';
import type { AgentInfo } from '../chatView/types';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

const AGENT = { id: 'task:run-1', description: 'a run' } as AgentInfo;

function renderWithChild(status: WorkflowChildStatus): void {
  const child: WorkflowChild = {
    seq: 0,
    status,
    label: 'child',
    phase: null,
    error: 'the reason it did not match',
  } as WorkflowChild;
  const run: WorkflowRunState = {
    ...createWorkflowRunState(),
    status: 'completed',
    children: [child],
  };
  render(
    <WorkflowRunContext.Provider value={() => run}>
      <WorkflowRunDetail agent={AGENT} />
    </WorkflowRunContext.Provider>,
  );
}

describe('WorkflowRunDetail child error colour', () => {
  it('tints a schema miss amber, matching its own row', () => {
    renderWithChild('invalid_schema');
    expect(screen.getByTestId('workflow-detail-child-error')).toHaveStyle({
      color: 'var(--color-warning)',
    });
  });

  it('still tints a genuine failure as loss', () => {
    renderWithChild('error');
    expect(screen.getByTestId('workflow-detail-child-error')).toHaveStyle({
      color: 'var(--color-loss)',
    });
  });
});

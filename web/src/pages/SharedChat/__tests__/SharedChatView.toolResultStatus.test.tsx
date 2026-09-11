/**
 * The public replay is a fourth construction site for the tool-result record,
 * and the only one with no live stream behind it. The regression this guards:
 * a rejected direct order rendered on a shared link as a successful call,
 * because `status` was dropped here and LangChain's rejection prose ("User
 * rejected the tool call") matches none of the failure-prose rules.
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/react';

const capturedMessages: Record<string, unknown>[][] = [];

vi.mock('react-router-dom', () => ({
  useParams: () => ({ shareToken: 'tok' }),
  Link: ({ children }: { children?: React.ReactNode }) => <span>{children}</span>,
}));

vi.mock('../../../contexts/ThemeContext', () => ({
  useTheme: () => ({ theme: 'light' }),
}));

vi.mock('../../ChatAgent/components/MessageList', () => ({
  default: ({ messages }: { messages: Record<string, unknown>[] }) => {
    capturedMessages.push(messages);
    return <div data-testid="message-list" />;
  },
}));

vi.mock('../../ChatAgent/components/FilePanel', () => ({ default: () => null }));

vi.mock('../../ChatAgent/contexts/WorkspaceContext', () => ({
  WorkspaceProvider: ({ children }: { children?: React.ReactNode }) => <>{children}</>,
}));

const replayEvents = [
  { event: 'user_message', turn_index: 0, role: 'user', content: 'sell 10 AAPL' },
  {
    event: 'tool_calls',
    turn_index: 0,
    tool_calls: [{ id: 'tc1', name: 'mcp__moomoo__place_order', args: {} }],
  },
  {
    event: 'tool_call_result',
    turn_index: 0,
    tool_call_id: 'tc1',
    content: 'User rejected the tool call with reason: not now',
    content_type: 'text',
    status: 'error',
  },
  { event: 'replay_done' },
];

vi.mock('../api', () => ({
  getSharedThread: vi.fn(async () => ({ title: 'Shared', workspace_name: null })),
  replaySharedThread: vi.fn(async (_token: string, onEvent: (e: unknown) => void) => {
    replayEvents.forEach(onEvent);
  }),
  getSharedFiles: vi.fn(async () => []),
  readSharedFile: vi.fn(async () => ''),
  downloadSharedFileAs: vi.fn(async () => undefined),
  fetchSharedServeObjectUrl: vi.fn(async () => ''),
  fetchSharedServeArrayBuffer: vi.fn(async () => new ArrayBuffer(0)),
}));

import SharedChatView from '../SharedChatView';

describe('SharedChatView tool result replay', () => {
  beforeEach(() => {
    capturedMessages.length = 0;
  });

  it('carries the wire status so a rejected call replays as a failure', async () => {
    render(<SharedChatView />);

    await waitFor(() => {
      expect(capturedMessages.length).toBeGreaterThan(0);
    });

    await waitFor(() => {
      const latest = capturedMessages[capturedMessages.length - 1];
      const assistant = latest.find(
        (m) => (m.toolCallProcesses as Record<string, unknown> | undefined)?.tc1,
      );
      const process = (assistant?.toolCallProcesses as Record<string, Record<string, unknown>>).tc1;
      expect(process.isFailed).toBe(true);
    });
  });
});

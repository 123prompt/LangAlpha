/**
 * A shared Flash transcript relays worker deliverables, and the secretary
 * qualifies those links with the workspace that holds them. The share token
 * authorizes this thread's workspace alone, so a card naming another one has
 * no bytes to save here: resolving its name against the shared workspace would
 * look it up in the wrong place and save whatever namesake it found.
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import type { MessageActions } from '../../ChatAgent/components/messageList/MessageActionsContext';

let captured: MessageActions | null = null;

vi.mock('react-router-dom', () => ({
  Link: ({ children }: { children?: React.ReactNode }) => <span>{children}</span>,
}));

vi.mock('../../../contexts/AuthContext', () => ({ useAuth: () => ({ isLoggedIn: false }) }));
vi.mock('../../../contexts/ThemeContext', () => ({ useTheme: () => ({ theme: 'light' }) }));

vi.mock('../../ChatAgent/components/MessageList', async () => {
  const { useMessageActions } = await import(
    '../../ChatAgent/components/messageList/MessageActionsContext'
  );
  // Named and capitalized so `rules-of-hooks` reads it as the component it is.
  const MessageListProbe = () => {
    captured = useMessageActions();
    return <div data-testid="message-list" />;
  };
  return { default: MessageListProbe };
});

vi.mock('../../ChatAgent/components/FilePanel', () => ({ default: () => null }));
vi.mock('../../ChatAgent/contexts/WorkspaceContext', () => ({
  WorkspaceProvider: ({ children }: { children?: React.ReactNode }) => <>{children}</>,
}));

// Hoisted with the `vi.mock` factory below, which runs before this module's
// own top-level statements.
const { resolveSharedFile, downloadSharedFile } = vi.hoisted(() => ({
  resolveSharedFile: vi.fn(async () => ({
    status: 'resolved',
    path: 'results/report.md',
    matches: ['results/report.md'],
  })),
  downloadSharedFile: vi.fn(async () => undefined),
}));

vi.mock('../api', () => ({
  replaySharedThread: vi.fn(async (_token: string, onEvent: (e: unknown) => void) => {
    onEvent({ event: 'user_message', turn_index: 0, role: 'user', content: 'build it' });
    onEvent({ event: 'replay_done' });
  }),
  getSharedFiles: vi.fn(async () => ({ files: [] })),
  readSharedFile: vi.fn(async () => ''),
  resolveSharedFile,
  downloadSharedFile,
  servedObjectUrl: vi.fn(async () => ''),
  servedBytes: vi.fn(async () => new ArrayBuffer(0)),
  sharedServePrefix: (token: string) => `/api/v1/public/shared/${token}/files/serve/`,
}));

import SharedChatView from '../SharedChatView';
import type { SharedThreadMetadata } from '../api';

const metadata = { kind: 'thread', thread_id: 't1', title: 'Shared', workspace_name: '', msg_type: 'chat', created_at: '', updated_at: '', permissions: { allow_files: true, allow_download: true } } as SharedThreadMetadata;

async function actions(): Promise<MessageActions> {
  render(<SharedChatView shareToken="tok" metadata={metadata} />);
  await waitFor(() => expect(captured).not.toBeNull());
  return captured!;
}

describe('SharedChatView deliverable download', () => {
  beforeEach(() => {
    captured = null;
    resolveSharedFile.mockClear();
    downloadSharedFile.mockClear();
  });

  it('does not resolve or save a card that names another workspace', async () => {
    const { onDownloadFile } = await actions();
    await onDownloadFile!('results/report.md', 'other-workspace-id');
    expect(resolveSharedFile).not.toHaveBeenCalled();
    expect(downloadSharedFile).not.toHaveBeenCalled();
  });

  it('saves a card from this thread s own workspace', async () => {
    // The control that makes the line above mean something: the same handler,
    // the same path, no qualifier, and the save goes through.
    const { onDownloadFile } = await actions();
    await onDownloadFile!('results/report.md');
    expect(resolveSharedFile).toHaveBeenCalledWith('tok', ['results/report.md'], []);
    expect(downloadSharedFile).toHaveBeenCalledWith('tok', 'results/report.md');
  });
});

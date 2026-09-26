/**
 * A turn's end is when the server measures the machine's disk, onto the
 * computer row; the open page has no other reason to re-read that row, so the
 * stream's end has to ask for it. Once per turn, and only where there is a
 * machine: a flash thread has none.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { Mock } from 'vitest';
import { act, waitFor } from '@testing-library/react';
import { renderHookWithProviders } from '@/test/utils';
import { settleMountEffect } from './chatHookHarness';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));

vi.mock('@/lib/supabase', () => ({ supabase: null }));

vi.mock('../utils/threadStorage', () => ({
  getStoredThreadId: vi.fn().mockReturnValue(null),
  setStoredThreadId: vi.fn(),
  removeStoredThreadId: vi.fn(),
}));

vi.mock('../../utils/api', async () => (await import('./chatHookHarness')).apiMockModule());

vi.mock('../useComputers', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../useComputers')>()),
  refreshComputersAfterTurn: vi.fn(),
}));

import { getWorkflowStatus, replayThreadHistory, sendChatMessageStream } from '../../utils/api';
import { refreshComputersAfterTurn } from '../useComputers';
import { useChatMessages } from '../useChatMessages';

const mockStatus = getWorkflowStatus as Mock;
const mockReplay = replayThreadHistory as Mock;
const mockSend = sendChatMessageStream as Mock;
const mockRefresh = refreshComputersAfterTurn as Mock;

/** Mount a thread and run one turn that streams a line and completes. */
async function completeOneTurn(agentMode: string) {
  mockSend.mockImplementation(async (...args: unknown[]) => {
    const onEvent = args[5] as (e: Record<string, unknown>) => void;
    onEvent({ event: 'message_chunk', role: 'assistant', agent: 'main', content_type: 'text', content: 'done' });
    return { disconnected: false };
  });
  const rendered = renderHookWithProviders(() =>
    useChatMessages('ws-x', 'th-x', null, null, null, null, null, null, agentMode),
  );
  await waitFor(() => expect(mockReplay).toHaveBeenCalled());
  await settleMountEffect();
  await act(async () => {
    await rendered.result.current.handleSendMessage('measure', false);
  });
  await waitFor(() => expect(rendered.result.current.isLoading).toBe(false));
  return rendered;
}

describe('useChatMessages, turn end re-reads the machine rows', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockReplay.mockReset();
    mockReplay.mockResolvedValue(undefined);
    mockSend.mockReset();
    mockStatus.mockReset();
    mockStatus.mockResolvedValue({ can_reconnect: false, status: 'completed' });
  });

  it('asks for the rows once when a PTC turn completes', async () => {
    const { queryClient } = await completeOneTurn('ptc');
    expect(mockRefresh).toHaveBeenCalledTimes(1);
    expect(mockRefresh).toHaveBeenCalledWith(queryClient);
  });

  it('asks for nothing on a flash thread, which has no machine', async () => {
    await completeOneTurn('flash');
    expect(mockRefresh).not.toHaveBeenCalled();
  });
});

import type { ReactNode } from 'react';
import { act, renderHook } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { expect, it, vi } from 'vitest';
import { queryKeys } from '@/lib/queryKeys';
import { useWorkspaceMutation } from '../useWorkspaceMutation';

it('refreshes the list and the mutated detail, and leaves siblings alone', async () => {
  const client = new QueryClient();
  const lists = queryKeys.workspaces.lists();
  const active = queryKeys.workspaces.detail('active');
  const sibling = queryKeys.workspaces.detail('sibling');
  client.setQueryData(lists, []);
  client.setQueryData(active, { name: 'old' });
  client.setQueryData(sibling, { name: 'other' });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const { result } = renderHook(() => useWorkspaceMutation<string>({
    mutationFn: vi.fn().mockResolvedValue({}),
    errorTitleKey: 'workspace.renameFailed',
  }), { wrapper });
  await act(async () => { expect(await result.current.run('active', 'new')).toBe(true); });
  expect(client.getQueryState(lists)?.isInvalidated).toBe(true);
  expect(client.getQueryState(active)?.isInvalidated).toBe(true);
  expect(client.getQueryState(sibling)?.isInvalidated).toBe(false);
  client.clear();
});

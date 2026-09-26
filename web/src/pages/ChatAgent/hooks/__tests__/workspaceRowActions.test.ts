import { QueryClient } from '@tanstack/react-query';
import { expect, it } from 'vitest';

import { queryKeys } from '@/lib/queryKeys';
import { invalidateWorkspaceMembership } from '../workspaceRowActions';

it('invalidates workspace and computer projections after membership changes', () => {
  const client = new QueryClient();
  const workspaces = queryKeys.workspaces.lists();
  const computers = queryKeys.computers.lists();
  // Shares the `computers` prefix, and costs a `du` on the machine: a
  // membership change must not re-read it.
  const storage = queryKeys.computers.storage('c1');
  client.setQueryData(workspaces, { workspaces: [] });
  client.setQueryData(computers, { computers: [] });
  client.setQueryData(storage, { live: true, workspaces: [], other_bytes: 0 });

  invalidateWorkspaceMembership(client);

  expect(client.getQueryState(workspaces)?.isInvalidated).toBe(true);
  expect(client.getQueryState(computers)?.isInvalidated).toBe(true);
  expect(client.getQueryState(storage)?.isInvalidated).toBe(false);
  client.clear();
});

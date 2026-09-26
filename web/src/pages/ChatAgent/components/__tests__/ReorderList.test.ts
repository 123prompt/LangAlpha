import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';

vi.mock('../../utils/api', () => ({
  getWorkspaces: vi.fn(),
  reorderWorkspaces: vi.fn(),
}));

import { getWorkspaces } from '../../utils/api';
import {
  getAllReorderWorkspaces,
  getAllWorkspaces,
} from '../workspaceGallery/loadReorderWorkspaces';

const mockGetWorkspaces = getWorkspaces as Mock;

function workspace(index: number) {
  return {
    workspace_id: `workspace-${index}`,
    name: `Workspace ${index}`,
    status: 'stopped',
    updated_at: '2026-09-20T00:00:00Z',
  };
}

describe('getAllReorderWorkspaces', () => {
  beforeEach(() => vi.clearAllMocks());

  it('loads every page before exposing the reorder list', async () => {
    const firstPage = Array.from({ length: 100 }, (_, index) => workspace(index));
    const secondPage = [workspace(100), workspace(101)];
    mockGetWorkspaces
      .mockResolvedValueOnce({ workspaces: firstPage, total: 102 })
      .mockResolvedValueOnce({ workspaces: secondPage, total: 102 });

    const rows = await getAllReorderWorkspaces();

    expect(rows).toHaveLength(102);
    expect(mockGetWorkspaces).toHaveBeenNthCalledWith(1, 100, 0, 'custom', false);
    expect(mockGetWorkspaces).toHaveBeenNthCalledWith(2, 100, 100, 'custom', false);
  });

  it('loads every page for gallery search with its active sort and Flash', async () => {
    const firstPage = Array.from({ length: 100 }, (_, index) => workspace(index));
    const secondPage = [workspace(100)];
    mockGetWorkspaces
      .mockResolvedValueOnce({ workspaces: firstPage, total: 101 })
      .mockResolvedValueOnce({ workspaces: secondPage, total: 101 });

    const rows = await getAllWorkspaces('name', true);

    expect(rows).toHaveLength(101);
    expect(mockGetWorkspaces).toHaveBeenNthCalledWith(1, 100, 0, 'name', true);
    expect(mockGetWorkspaces).toHaveBeenNthCalledWith(2, 100, 100, 'name', true);
  });
});

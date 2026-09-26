import { getWorkspaces } from '../../utils/api';
import type { WorkspaceRecord } from './types';

const REORDER_PAGE_SIZE = 100;

export async function getAllWorkspaces(
  sortBy: string,
  includeFlash: boolean,
): Promise<WorkspaceRecord[]> {
  const rows: WorkspaceRecord[] = [];

  while (true) {
    const page = await getWorkspaces(
      REORDER_PAGE_SIZE,
      rows.length,
      sortBy,
      includeFlash,
    );
    rows.push(...page.workspaces as WorkspaceRecord[]);
    const reachedTotal = typeof page.total === 'number' && rows.length >= page.total;
    if (reachedTotal || page.workspaces.length < REORDER_PAGE_SIZE) return rows;
  }
}

export function getAllReorderWorkspaces(): Promise<WorkspaceRecord[]> {
  return getAllWorkspaces('custom', false);
}

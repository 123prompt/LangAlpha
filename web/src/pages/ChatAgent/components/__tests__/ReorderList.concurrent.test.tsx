import { act, screen } from '@testing-library/react';
import type { DragEndEvent } from '@dnd-kit/core';
import type { Mock } from 'vitest';
import { beforeEach, expect, it, vi } from 'vitest';

import { renderWithProviders } from '@/test/utils';

const dnd = vi.hoisted(() => ({
  onDragEnd: null as ((event: DragEndEvent) => Promise<void>) | null,
}));

vi.mock('@dnd-kit/core', async () => {
  const React = await import('react');
  return {
    DndContext: ({ children, onDragEnd }: {
      children: React.ReactNode;
      onDragEnd: (event: DragEndEvent) => Promise<void>;
    }) => {
      dnd.onDragEnd = onDragEnd;
      return React.createElement('div', null, children);
    },
    PointerSensor: class {},
    closestCenter: vi.fn(),
    useSensor: vi.fn(() => ({})),
    useSensors: vi.fn(() => []),
  };
});

vi.mock('@dnd-kit/sortable', async () => {
  const React = await import('react');
  return {
    SortableContext: ({ children }: { children: React.ReactNode }) =>
      React.createElement('div', null, children),
    arrayMove: <T,>(rows: T[], from: number, to: number) => {
      const copy = [...rows];
      const [row] = copy.splice(from, 1);
      copy.splice(to, 0, row);
      return copy;
    },
    useSortable: vi.fn(() => ({
      attributes: {},
      listeners: {},
      setNodeRef: vi.fn(),
      transform: null,
      transition: undefined,
      isDragging: false,
    })),
    verticalListSortingStrategy: {},
  };
});

vi.mock('@dnd-kit/utilities', () => ({ CSS: { Transform: { toString: () => undefined } } }));

vi.mock('../../utils/api', () => ({ reorderWorkspaces: vi.fn() }));
vi.mock('../workspaceGallery/loadReorderWorkspaces', () => ({
  getAllReorderWorkspaces: vi.fn(async () => [
    {
      workspace_id: 'workspace-a',
      name: 'Alpha',
      status: 'stopped',
      sort_order: 0,
      updated_at: '2026-09-21T00:00:00Z',
    },
    {
      workspace_id: 'workspace-b',
      name: 'Beta',
      status: 'stopped',
      sort_order: 1,
      updated_at: '2026-09-21T00:00:00Z',
    },
  ]),
}));

import { reorderWorkspaces } from '../../utils/api';
import { ReorderList } from '../workspaceGallery/ReorderList';

const mockReorder = reorderWorkspaces as Mock;

beforeEach(() => {
  vi.clearAllMocks();
  dnd.onDragEnd = null;
});

it('ignores another completed drag until the current order is durable', async () => {
  let finishWrite!: () => void;
  mockReorder.mockReturnValue(new Promise<void>((resolve) => { finishWrite = resolve; }));
  renderWithProviders(<ReorderList flashWorkspace={null} onDone={vi.fn()} />);

  await screen.findByText('Alpha');
  let first: Promise<void> | undefined;
  act(() => {
    first = dnd.onDragEnd?.({
      active: { id: 'workspace-a' },
      over: { id: 'workspace-b' },
    } as DragEndEvent);
  });
  await act(async () => { await Promise.resolve(); });
  expect(screen.getByRole('button', { name: /done/i })).toBeDisabled();

  await act(async () => {
    await dnd.onDragEnd?.({
      active: { id: 'workspace-b' },
      over: { id: 'workspace-a' },
    } as DragEndEvent);
  });
  expect(mockReorder).toHaveBeenCalledTimes(1);

  await act(async () => {
    finishWrite();
    await first;
  });
  expect(screen.getByRole('button', { name: /done/i })).not.toBeDisabled();
});

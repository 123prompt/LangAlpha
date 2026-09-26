/**
 * Spec-change outcomes are announced off the list itself, so whichever read
 * sees a change settle (the poll, window focus, a status reconcile, an
 * invalidation) is the one that reports it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { Mock } from 'vitest';
import { act, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { renderWithProviders } from '@/test/utils';
import { queryKeys } from '@/lib/queryKeys';
import type { Computer, ComputerSpecChange, ComputersResponse } from '@/types/api';

vi.mock('@/config/hostMode', () => ({
  HOST_MODE: 'oss',
  isPlatformMode: false,
  APP_ENTRY_PATH: '/',
}));

vi.mock('@/components/ui/use-toast', () => ({ toast: vi.fn() }));

vi.mock('../../utils/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../utils/api')>()),
  getComputers: vi.fn(),
  getComputerStorage: vi.fn(),
  setComputerSpec: vi.fn(),
  getWorkspaceQuota: vi.fn(async () => ({ performance: null, max: null, always_on: null })),
  streamComputerEvents: vi.fn(async () => {}),
  streamWorkspaceEvents: vi.fn(async () => {}),
}));

import { toast } from '@/components/ui/use-toast';
import { getComputers, setComputerSpec } from '../../utils/api';
import { closeComputerSpec, openComputerSpec } from '../../hooks/computerPanelStore';
import ComputersDialogHost from '../ComputersDialogHost';

const mockToast = toast as unknown as Mock;
const mockGetComputers = getComputers as Mock;

const ID = '5319ad0e-7835-4ca6-9541-b7c2961a7bf6';
const STARTED = '2026-09-24T18:00:00Z';

function machine(spec_change: ComputerSpecChange | null, status = 'running'): Computer {
  return {
    computer_id: ID,
    user_id: 'u1',
    kind: 'daytona',
    name: 'Alpha Research',
    status,
    resource_tier: 'standard',
    is_always_on: false,
    is_primary: true,
    workspace_count: 1,
    root_dir: '/home/workspace',
    spec_change,
  } as Computer;
}

function change(overrides: Partial<ComputerSpecChange> = {}): ComputerSpecChange {
  return {
    target_tier: 'performance',
    from_tier: 'standard',
    state: 'in_progress',
    started_at: STARTED,
    ...overrides,
  };
}

function list(computer: Computer): ComputersResponse {
  return { computers: [computer], total: 1 };
}

/** Any read of the list: here an invalidation, as window focus or a reconcile would do. */
async function refetchReturning(queryClient: ReturnType<typeof renderWithProviders>['queryClient'], computer: Computer) {
  mockGetComputers.mockResolvedValue(list(computer));
  await act(async () => {
    await queryClient.invalidateQueries({ queryKey: queryKeys.computers.lists() });
  });
}

/** Render with the first list read landed, so the host has seen what it holds. */
async function mount() {
  const rendered = renderWithProviders(<ComputersDialogHost />);
  await waitFor(() =>
    expect(rendered.queryClient.getQueryData(queryKeys.computers.lists())).toBeDefined(),
  );
  await act(async () => {});
  return rendered;
}

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  act(() => closeComputerSpec());
});

describe('ComputersDialogHost, spec-change outcomes', () => {
  it('stays quiet about an outcome that settled before the page loaded', async () => {
    mockGetComputers.mockResolvedValue(list(machine(change({ state: 'succeeded' }))));
    await mount();
    expect(mockToast).not.toHaveBeenCalled();
  });

  it('announces a settled row that arrives through any refetch, not only its own poll', async () => {
    mockGetComputers.mockResolvedValue(list(machine(change())));
    const { queryClient } = await mount();

    await refetchReturning(queryClient, machine(change({ state: 'succeeded' })));

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ description: 'Alpha Research now runs at Performance.' }),
      ),
    );
    expect(mockToast).toHaveBeenCalledTimes(1);
  });

  it('announces once, however many reads follow', async () => {
    mockGetComputers.mockResolvedValue(list(machine(change())));
    const { queryClient } = await mount();

    await refetchReturning(queryClient, machine(change({ state: 'succeeded' })));
    await waitFor(() => expect(mockToast).toHaveBeenCalledTimes(1));
    await refetchReturning(queryClient, machine(change({ state: 'succeeded', target_tier: 'performance' }), 'stopped'));
    expect(mockToast).toHaveBeenCalledTimes(1);
  });

  it('waits past a stale row that predates the change instead of reading it as the outcome', async () => {
    mockGetComputers.mockResolvedValue(list(machine(change())));
    const { queryClient } = await mount();

    await refetchReturning(
      queryClient,
      machine(change({ state: 'failed', started_at: '2026-09-01T00:00:00Z', error: { code: 'busy', message: 'x', files: [] } })),
    );
    expect(mockToast).not.toHaveBeenCalled();

    await refetchReturning(queryClient, machine(change({ state: 'succeeded' })));
    await waitFor(() => expect(mockToast).toHaveBeenCalledTimes(1));
  });

  it("relays the server's sentence for a downgrade that does not fit, as a toast while the dialog is closed", async () => {
    const message =
      'Cannot downgrade: files on this computer (2.5 GiB) exceed what the Standard spec holds. Free up space first.';
    mockGetComputers.mockResolvedValue(list(machine(change({ target_tier: 'standard', from_tier: 'performance' }))));
    const { queryClient } = await mount();

    await refetchReturning(
      queryClient,
      machine(change({ state: 'failed', error: { code: 'disk_too_small', message, files: [] } })),
    );

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: 'destructive',
          title: 'Could not change the spec of Alpha Research',
          description: message,
        }),
      ),
    );
  });

  it('shows progress in the open dialog, then the failure inline', async () => {
    mockGetComputers.mockResolvedValue(list(machine(change(), 'starting')));
    const { queryClient } = renderWithProviders(<ComputersDialogHost />);
    act(() => openComputerSpec(ID));

    expect(await screen.findByTestId('spec-change-progress')).toHaveTextContent('Upgrading to Performance…');

    await refetchReturning(
      queryClient,
      machine(change({ state: 'failed', error: { code: 'interrupted', message: 'server wording', files: [] } })),
    );

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(
      "The spec change was interrupted before it finished. Your files are safe. Check the computer's current spec and try again.",
    );
    expect(screen.queryByTestId('spec-change-progress')).not.toBeInTheDocument();
    expect(mockToast).not.toHaveBeenCalled();
  });

  it('closes the open dialog on success and says so', async () => {
    mockGetComputers.mockResolvedValue(list(machine(change(), 'starting')));
    const { queryClient } = renderWithProviders(<ComputersDialogHost />);
    act(() => openComputerSpec(ID));
    await screen.findByTestId('spec-change-progress');

    await refetchReturning(queryClient, machine(change({ state: 'succeeded' })));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({ description: 'Alpha Research now runs at Performance.' }),
    );
  });

  it('closes the dialog when the server answers 200, already at that tier', async () => {
    mockGetComputers.mockResolvedValue(list(machine(null)));
    (setComputerSpec as Mock).mockResolvedValue({ ...machine(null), resource_tier: 'performance' });
    await mount();
    act(() => openComputerSpec(ID));

    await userEvent.click(await screen.findByRole('radio', { name: /^Performance/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Back up and change spec' }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(mockToast).toHaveBeenCalledWith(expect.objectContaining({ description: 'Performance' }));
  });
});

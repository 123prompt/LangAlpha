/**
 * Computer endpoints: the machine a workspace runs on.
 *
 * `POST /computers` is the only route here that allocates anything, so it is
 * the only one that can be refused for capacity; every other call is about a
 * machine the user already has.
 */
import { api } from '@/api/client';
import type {
  Computer,
  ComputerActionResponse,
  ComputerCreate,
  ComputerStorage,
  ComputersResponse,
  ResourceTier,
  UnsavedFile,
} from '@/types/api';

import { apiErrorDetail, apiErrorStatus } from './errors';
import { streamStatusEvents } from './statusStream';

// Matches the workspace module's bounds: starting a machine legitimately runs
// tens of seconds, while a read that hangs should surface as a failure.
const COMPUTER_MUTATION_TIMEOUT_MS = 120000;
// `du` across every folder on the machine. The server bounds the reading at the
// exec's 30 s plus a 5 s connect, then answers the stored reading instead of
// failing; wait past that so a slow machine shows its last reading.
const COMPUTER_STORAGE_TIMEOUT_MS = 45000;

export async function getComputers(): Promise<ComputersResponse> {
  const { data } = await api.get<ComputersResponse>('/api/v1/computers');
  return data;
}

/**
 * Create a computer. The tier is picked here because it is the machine's
 * property, not a project's. In platform mode this is the metered route: a
 * refusal arrives as a 429 whose `detail.message` is the platform's own
 * wording, which the shared axios interceptor puts on `err.rateLimitInfo`.
 */
export async function createComputer(body: ComputerCreate = {}): Promise<Computer> {
  const { data } = await api.post<Computer>(
    '/api/v1/computers',
    {
      ...(body.name ? { name: body.name } : {}),
      resource_tier: body.resource_tier ?? 'standard',
    },
    { timeout: COMPUTER_MUTATION_TIMEOUT_MS },
  );
  return data;
}

/**
 * Start a stopped computer. `{ lazy: true }` returns 202 immediately with
 * status 'starting' and continues in the background, which is what a UI that
 * then watches the status stream wants.
 */
export async function startComputer(
  computerId: string,
  opts: { lazy?: boolean } = {},
): Promise<ComputerActionResponse> {
  if (!computerId) throw new Error('Computer ID is required');
  const params = opts.lazy ? '?lazy=true' : '';
  const { data } = await api.post<ComputerActionResponse>(
    `/api/v1/computers/${computerId}/start${params}`,
    null,
    { timeout: COMPUTER_MUTATION_TIMEOUT_MS },
  );
  return data;
}

/** Stop a running computer, preserving its files. Every workspace on it stops with it. */
export async function stopComputer(computerId: string): Promise<ComputerActionResponse> {
  if (!computerId) throw new Error('Computer ID is required');
  const { data } = await api.post<ComputerActionResponse>(
    `/api/v1/computers/${computerId}/stop`,
    null,
    { timeout: COMPUTER_MUTATION_TIMEOUT_MS },
  );
  return data;
}

export async function renameComputer(computerId: string, name: string): Promise<Computer> {
  if (!computerId) throw new Error('Computer ID is required');
  const { data } = await api.patch<Computer>(`/api/v1/computers/${computerId}`, { name });
  return data;
}

/** The structured `detail` of a 409 from the spec route. */
export interface SpecConflictDetail {
  code: 'turn_active' | 'spec_in_progress' | 'busy' | 'backup_incomplete';
  message: string;
  files?: UnsavedFile[];
}

/** The 409 a spec request was refused with, or null for any other failure. */
export function specConflictDetail(err: unknown): SpecConflictDetail | null {
  if (apiErrorStatus(err) !== 409) return null;
  const detail = apiErrorDetail(err);
  // The server's contract for this route; narrowed once, here.
  return detail && typeof detail === 'object' && 'code' in detail ? (detail as SpecConflictDetail) : null;
}

/**
 * Ask for a tier change. The server answers 202 once the change is accepted
 * and runs it afterwards (a backup and a recreate take minutes), so the
 * returned row carries `spec_change` in progress and its outcome is read back
 * from the computer. A 200 means the machine is already at that tier. Up-front
 * refusals arrive as 403/429 (plan and quota, platform mode), or as a 409 with
 * a `SpecConflictDetail` (a turn running, a change or other operation already
 * in progress, a backup that could not finish).
 */
export async function setComputerSpec(computerId: string, tier: ResourceTier): Promise<Computer> {
  if (!computerId) throw new Error('Computer ID is required');
  const { data } = await api.post<Computer>(
    `/api/v1/computers/${computerId}/spec`,
    { tier },
    { timeout: COMPUTER_MUTATION_TIMEOUT_MS },
  );
  return data;
}

/** Keep the machine running (idle auto-stop off). Enabling on a stopped machine starts it. */
export async function setComputerAlwaysOn(computerId: string, enabled: boolean): Promise<Computer> {
  if (!computerId) throw new Error('Computer ID is required');
  const { data } = await api.post<Computer>(
    `/api/v1/computers/${computerId}/always-on`,
    { enabled },
    { timeout: COMPUTER_MUTATION_TIMEOUT_MS },
  );
  return data;
}

/**
 * Disk reading plus a per-workspace breakdown. On a running machine this
 * measures now (and the server stores the reading on the row); otherwise it
 * returns the last stored reading with no breakdown.
 */
export async function getComputerStorage(computerId: string): Promise<ComputerStorage> {
  if (!computerId) throw new Error('Computer ID is required');
  const { data } = await api.get<ComputerStorage>(`/api/v1/computers/${computerId}/storage`, {
    timeout: COMPUTER_STORAGE_TIMEOUT_MS,
  });
  return data;
}

/**
 * Subscribe to a computer's lifecycle status via SSE. Same frames, keepalive
 * and 600 s cap as the workspace channel, and the same best-effort contract:
 * resolves on a terminal status, the server's timeout, or an abort, and never
 * throws on a network error.
 *
 * This is the authoritative channel for every workspace on the machine: one
 * subscription answers for all of them, where the workspace channel would open
 * one connection per project to report the same transition.
 */
export async function streamComputerEvents(
  computerId: string,
  onStatus: (status: string, sandboxState?: string) => void,
  signal: AbortSignal,
): Promise<void> {
  if (!computerId) return;
  await streamStatusEvents(
    `/api/v1/computers/${computerId}/events`,
    (frame) => onStatus(frame.status, frame.sandbox_state),
    signal,
  );
}

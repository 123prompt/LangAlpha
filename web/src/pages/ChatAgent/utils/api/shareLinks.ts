/**
 * Share links and file grants (owner side).
 *
 * A leaf on purpose: consumers import this module directly rather than through
 * `utils/api.ts`, so the whole-module mock factories on that barrel keep
 * working without naming these.
 */
import { api } from '@/api/client';
import { apiErrorDetail, apiErrorStatus } from './errors';
import type {
  FileGrant,
  ShareLink,
  ShareLinkFiles,
  ShareLinkTarget,
  SharedLinksResponse,
} from '@/types/api';

export async function createShareLink(workspaceId: string, target: ShareLinkTarget): Promise<ShareLink> {
  const { data } = await api.post<ShareLink>(`/api/v1/workspaces/${workspaceId}/share-links`, target);
  return data;
}

export async function getShareLinkFiles(workspaceId: string, code: string): Promise<ShareLinkFiles> {
  const { data } = await api.get<ShareLinkFiles>(`/api/v1/workspaces/${workspaceId}/share-links/${code}/files`);
  return data;
}

export type ShareLinkPatch = { shared: true; files: string[] } | { shared: false };

export async function patchShareLink(workspaceId: string, code: string, patch: ShareLinkPatch): Promise<ShareLink> {
  const { data } = await api.patch<ShareLink>(`/api/v1/workspaces/${workspaceId}/share-links/${code}`, patch);
  return data;
}

export async function listSharedLinks(workspaceId: string): Promise<SharedLinksResponse> {
  const { data } = await api.get<SharedLinksResponse>(`/api/v1/workspaces/${workspaceId}/share-links`);
  return data;
}

export async function createFileGrant(workspaceId: string): Promise<FileGrant> {
  const { data } = await api.post<FileGrant>(`/api/v1/workspaces/${workspaceId}/file-grant`);
  return data;
}

/**
 * The absolute link a user pastes somewhere: the app origin plus `/a/<code>`.
 * An app's `path` opens it at that page rather than the link's own entry,
 * which moves whenever the agent publishes another page on the same port.
 */
export function shareLinkHref(code: string, path?: string | null): string {
  const page = path?.replace(/^\/+/, '');
  return `${window.location.origin}/a/${code}${page ? `?${new URLSearchParams({ path: page })}` : ''}`;
}

/** Which 409 a share PATCH answered: the reviewed list no longer matches the file, or the link changed during the request. */
export type ShareConflict = 'files_changed' | 'link_changed';

export function shareConflictIn(err: unknown): ShareConflict | null {
  if (apiErrorStatus(err) !== 409) return null;
  const detail = apiErrorDetail(err);
  const code = detail && typeof detail === 'object' ? (detail as { code?: unknown }).code : undefined;
  return code === 'files_changed' || code === 'link_changed' ? code : null;
}

/** Which cap a file's list went over: a count of files, or their total bytes. */
export interface ShareCap {
  code: 'too_many_files' | 'too_many_bytes';
  limit: number;
}

/** The cap a 422 names, or null for any other error. */
export function shareCapIn(err: unknown): ShareCap | null {
  if (apiErrorStatus(err) !== 422) return null;
  const detail = apiErrorDetail(err);
  if (!detail || typeof detail !== 'object') return null;
  const { code, limit } = detail as { code?: unknown; limit?: unknown };
  if (code !== 'too_many_files' && code !== 'too_many_bytes') return null;
  return { code, limit: typeof limit === 'number' ? limit : 0 };
}

/**
 * User-level vault secrets (Connectors backing store). Mirrors the workspace
 * vault API but scoped to the signed-in user: these secrets back inherited
 * (user-level) MCP servers and are merged into every sandbox push, with
 * workspace secrets winning on name collision.
 */
import { api } from '@/api/client';

export interface UserVaultSecret {
  user_vault_secret_id: string;
  name: string;
  description: string;
  masked_value: string;
  created_at: string;
  updated_at: string;
}

export interface UserVaultSecretList {
  secrets: UserVaultSecret[];
  remaining_slots: number;
}

export async function getUserVaultSecrets(): Promise<UserVaultSecretList> {
  const { data } = await api.get<UserVaultSecretList>('/api/v1/mcp/vault/secrets');
  return { secrets: data.secrets ?? [], remaining_slots: data.remaining_slots ?? 0 };
}

export async function createUserVaultSecret(body: {
  name: string;
  value: string;
  description?: string;
}) {
  const { data } = await api.post('/api/v1/mcp/vault/secrets', body);
  return data as { name: string };
}

export async function updateUserVaultSecret(
  name: string,
  body: { value?: string; description?: string },
) {
  const { data } = await api.put(`/api/v1/mcp/vault/secrets/${name}`, body);
  return data as { name: string };
}

export async function revealUserVaultSecret(name: string): Promise<string> {
  const { data } = await api.get<{ value: string }>(
    `/api/v1/mcp/vault/secrets/${name}/reveal`,
  );
  return data.value;
}

export async function deleteUserVaultSecret(name: string) {
  const { data } = await api.delete(`/api/v1/mcp/vault/secrets/${name}`);
  return data as { ok: boolean };
}

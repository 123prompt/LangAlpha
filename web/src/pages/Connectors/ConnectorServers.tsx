import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { motion } from 'framer-motion';
import {
  AlertCircle,
  CheckCircle2,
  Download,
  Link2,
  Link2Off,
  MinusCircle,
  MoreVertical,
  Pencil,
  Plus,
  RefreshCw,
  Server,
  Trash2,
} from 'lucide-react';
import { Loader } from '@/components/ui/loader';
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
} from '@/components/ui/dropdown-menu';
import { toast } from '@/components/ui/use-toast';
import {
  useMcpCatalog,
  useCreateMcpCatalogServer,
  useUpdateMcpCatalogServer,
  useDeleteMcpCatalogServer,
  useToggleMcpCatalogServer,
  useImportMcpCatalogServers,
  useDisconnectMcpOauth,
  useRefreshMcpOauthSchemas,
} from '@/hooks/useMcpServers';
import { useUserVaultSecrets, useCreateUserVaultSecret } from '@/hooks/useUserVault';
import { McpServerModal } from '@/pages/ChatAgent/components/mcp/McpServerModal';
import { McpImportModal } from '@/pages/ChatAgent/components/mcp/McpImportModal';
import {
  formatApiErrorDetail,
  startMcpOauth,
  type CatalogServer,
  type EffectiveServer,
  type McpOauthStatus,
  type McpServerInput,
} from '@/pages/ChatAgent/utils/api';

/**
 * The Connectors → Servers tab: the user-level MCP server list. An enabled row
 * is inherited by EVERY workspace of the user; a disabled row is an inert
 * template. Remote (http) servers carry the OAuth connect lifecycle — the
 * vendor bearer never leaves the host, so "Connect" here is all a sandbox
 * needs for the server to work.
 */

// Matches the spring used by McpServerRow so the toggle feels identical.
const SPRING_SNAPPY = { type: 'spring' as const, stiffness: 200, damping: 22 };

interface OauthMeta {
  labelKey: string;
  color: string;
  bg: string;
  icon: React.ComponentType<{ className?: string }>;
}

const OAUTH_META: Record<McpOauthStatus, OauthMeta> = {
  connected: {
    labelKey: 'connectors.oauth.connected',
    color: 'var(--color-profit)',
    bg: 'var(--color-profit-soft)',
    icon: CheckCircle2,
  },
  needs_reauth: {
    labelKey: 'connectors.oauth.needsReauth',
    color: 'var(--color-warning, #d97706)',
    bg: 'var(--color-warning-soft)',
    icon: AlertCircle,
  },
  refresh_ambiguous: {
    labelKey: 'connectors.oauth.refreshAmbiguous',
    color: 'var(--color-warning, #d97706)',
    bg: 'var(--color-warning-soft)',
    icon: AlertCircle,
  },
  revoked: {
    labelKey: 'connectors.oauth.revoked',
    color: 'var(--color-text-tertiary)',
    bg: 'var(--color-bg-tag)',
    icon: MinusCircle,
  },
};

function OauthBadge({ status }: { status: McpOauthStatus }) {
  const { t } = useTranslation();
  const meta = OAUTH_META[status];
  if (!meta) return null;
  const Icon = meta.icon;
  return (
    <span
      className="inline-flex items-center gap-1 text-[0.6875rem] px-1.5 py-0.5 rounded font-medium"
      style={{ color: meta.color, backgroundColor: meta.bg }}
      data-testid={`oauth-status-${status}`}
    >
      <Icon className="h-3 w-3" />
      {t(meta.labelKey)}
    </span>
  );
}

/** Adapt a masked catalog row to the modal's `EffectiveServer`-shaped initial value. */
function catalogToInitial(c: CatalogServer): EffectiveServer {
  return {
    name: c.name,
    origin: 'workspace',
    transport: c.transport,
    enabled: true,
    editable: true,
    deletable: true,
    status: 'unknown',
    error: '',
    tool_count: 0,
    tools: [],
    missing_secrets: [],
    env_refs: c.env_refs,
    header_refs: c.header_refs,
    description: c.description,
    instruction: c.instruction,
    tool_exposure_mode: c.tool_exposure_mode,
    command: c.command,
    args: c.args,
    url: c.url,
    config_version: 0,
  };
}

/** Statuses whose next step is (re-)running the authorize flow. */
function needsConnect(status: McpOauthStatus | null | undefined): boolean {
  return !status || status === 'revoked' || status === 'needs_reauth' || status === 'refresh_ambiguous';
}

export function ConnectorServers() {
  const { t } = useTranslation();
  const { data: catalog, isLoading, error } = useMcpCatalog();
  const { data: vault } = useUserVaultSecrets();
  const createMutation = useCreateMcpCatalogServer();
  const updateMutation = useUpdateMcpCatalogServer();
  const deleteMutation = useDeleteMcpCatalogServer();
  const toggleMutation = useToggleMcpCatalogServer();
  const importMutation = useImportMcpCatalogServers();
  const disconnectMutation = useDisconnectMcpOauth();
  const refreshMutation = useRefreshMcpOauthSchemas();
  const createSecretMutation = useCreateUserVaultSecret();

  const [modalOpen, setModalOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [editing, setEditing] = useState<CatalogServer | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [togglingName, setTogglingName] = useState<string | null>(null);
  const [deletingName, setDeletingName] = useState<string | null>(null);
  const [connectingName, setConnectingName] = useState<string | null>(null);
  const [refreshingName, setRefreshingName] = useState<string | null>(null);

  const secretNames = (vault?.secrets ?? []).map((s) => s.name);
  const servers = catalog?.servers ?? [];
  const maxServers = catalog?.max_servers ?? 0;
  const atCap = maxServers > 0 && servers.length >= maxServers;

  async function handleSubmit(body: McpServerInput) {
    setSubmitError(null);
    try {
      const saved = editing
        ? await updateMutation.mutateAsync({ name: editing.name, body })
        : await createMutation.mutateAsync(body);
      setModalOpen(false);
      setEditing(null);
      if (saved.warnings?.length) {
        toast({
          title: t('connectors.servers.warningTitle'),
          description: saved.warnings.join('\n'),
        });
      }
    } catch (err) {
      setSubmitError(formatApiErrorDetail(err));
    }
  }

  async function handleToggle(server: CatalogServer, enabled: boolean) {
    setTogglingName(server.name);
    try {
      await toggleMutation.mutateAsync({ name: server.name, enabled });
    } catch (err) {
      toast({
        variant: 'destructive',
        title: t('connectors.servers.toggleFailed'),
        description: formatApiErrorDetail(err),
      });
    } finally {
      setTogglingName(null);
    }
  }

  async function handleDelete(name: string) {
    try {
      await deleteMutation.mutateAsync(name);
      setDeletingName(null);
    } catch (err) {
      toast({
        variant: 'destructive',
        title: t('connectors.servers.deleteFailed'),
        description: formatApiErrorDetail(err),
      });
    }
  }

  async function handleConnect(name: string) {
    setConnectingName(name);
    try {
      const { authorize_url } = await startMcpOauth(name, '/connectors');
      // Full-page navigation into the vendor's consent screen; the backend
      // callback lands back on /connectors with ?mcp_connected / ?mcp_error.
      window.location.assign(authorize_url);
    } catch (err) {
      setConnectingName(null);
      toast({
        variant: 'destructive',
        title: t('connectors.oauth.connectFailed'),
        description: formatApiErrorDetail(err),
      });
    }
  }

  async function handleDisconnect(name: string) {
    try {
      await disconnectMutation.mutateAsync(name);
      toast({
        title: t('connectors.oauth.disconnectedTitle'),
        description: t('connectors.oauth.disconnectedDesc', { server: name }),
      });
    } catch (err) {
      toast({
        variant: 'destructive',
        title: t('connectors.oauth.disconnectFailed'),
        description: formatApiErrorDetail(err),
      });
    }
  }

  async function handleRefreshSchemas(name: string) {
    setRefreshingName(name);
    try {
      const result = await refreshMutation.mutateAsync(name);
      if (result.status === 'ok') {
        toast({
          title: t('connectors.oauth.refreshedTitle'),
          description: t('connectors.oauth.refreshedDesc', {
            server: name,
            count: result.tool_count,
          }),
        });
      } else {
        toast({
          variant: 'destructive',
          title: t('connectors.oauth.refreshFailed'),
          description: result.error || result.status,
        });
      }
    } catch (err) {
      toast({
        variant: 'destructive',
        title: t('connectors.oauth.refreshFailed'),
        description: formatApiErrorDetail(err),
      });
    } finally {
      setRefreshingName(null);
    }
  }

  if (isLoading) {
    return (
      <div className="flex flex-col gap-2">
        {[1, 2, 3].map((i) => (
          <div key={i} className="h-14 rounded-lg animate-pulse" style={{ backgroundColor: 'var(--color-bg-card)' }} />
        ))}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Server className="h-4 w-4" style={{ color: 'var(--color-accent-primary)' }} />
          <span className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
            {t('connectors.servers.title')}
          </span>
          <span className="text-xs px-1.5 py-0.5 rounded" style={{ color: 'var(--color-text-tertiary)', backgroundColor: 'var(--color-bg-card)' }}>
            {servers.length} / {maxServers}
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            onClick={() => setImportOpen(true)}
            disabled={atCap}
            title={atCap ? t('connectors.servers.atCap', { max: maxServers }) : t('connectors.servers.importHint')}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-md transition-colors disabled:opacity-50"
            style={{ color: 'var(--color-text-secondary)', border: '1px solid var(--color-border-muted)' }}
          >
            <Download className="h-3 w-3" />
            {t('connectors.servers.importJson')}
          </button>
          <button
            type="button"
            onClick={() => { setEditing(null); setSubmitError(null); setModalOpen(true); }}
            disabled={atCap}
            title={atCap ? t('connectors.servers.atCap', { max: maxServers }) : undefined}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-md transition-colors disabled:opacity-50"
            style={{ color: 'var(--color-btn-primary-text)', backgroundColor: 'var(--color-btn-primary-bg)' }}
          >
            <Plus className="h-3 w-3" />
            {t('connectors.servers.addServer')}
          </button>
        </div>
      </div>

      <p className="text-[0.6875rem]" style={{ color: 'var(--color-text-tertiary)' }}>
        {t('connectors.servers.inheritHint')}
      </p>

      {error ? (
        <div className="text-xs p-2 rounded" style={{ backgroundColor: 'var(--color-bg-card)', color: 'var(--color-loss)' }}>
          {(error as { message?: string })?.message || t('connectors.servers.loadFailed')}
        </div>
      ) : servers.length === 0 ? (
        <div className="py-8 text-center text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
          {t('connectors.servers.empty')}
        </div>
      ) : (
        <div className="flex flex-col gap-1.5">
          {servers.map((server) => {
            const oauthEligible = server.transport === 'http';
            const status = server.oauth_status ?? null;
            return (
              <div
                key={server.name}
                className="flex items-start justify-between gap-3 py-2.5 px-3 rounded-lg"
                style={{ backgroundColor: 'var(--color-bg-card)' }}
                data-testid={`connector-row-${server.name}`}
              >
                <div className="min-w-0 flex flex-col gap-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <Server className="h-4 w-4 flex-shrink-0" style={{ color: 'var(--color-accent-primary)' }} />
                    <span className="text-sm font-medium truncate" style={{ color: 'var(--color-text-primary)' }}>
                      {server.name}
                    </span>
                    <span
                      className="text-[0.625rem] px-1.5 py-0.5 rounded uppercase tracking-wide"
                      style={{
                        color: 'var(--color-text-tertiary)',
                        backgroundColor: 'var(--color-bg-tag)',
                        border: '1px solid var(--color-border-muted)',
                      }}
                    >
                      {server.transport}
                    </span>
                    {status && <OauthBadge status={status} />}
                  </div>
                  {server.description && (
                    <p className="text-[0.6875rem] line-clamp-2" style={{ color: 'var(--color-text-tertiary)' }}>
                      {server.description}
                    </p>
                  )}
                  <span className="text-[0.6875rem]" style={{ color: server.enabled ? 'var(--color-text-secondary)' : 'var(--color-text-tertiary)' }}>
                    {server.enabled
                      ? t('connectors.servers.enabledState')
                      : t('connectors.servers.disabledState')}
                  </span>
                </div>

                <div className="flex items-center gap-2 flex-shrink-0">
                  {oauthEligible && needsConnect(status) && (
                    <button
                      type="button"
                      onClick={() => handleConnect(server.name)}
                      disabled={connectingName === server.name}
                      className="inline-flex items-center gap-1 px-2 py-1 text-[0.6875rem] rounded-md transition-colors disabled:opacity-50"
                      style={{ color: 'var(--color-text-primary)', border: '1px solid var(--color-border-muted)' }}
                    >
                      {connectingName === server.name
                        ? <Loader size={12} className="text-current" />
                        : <Link2 className="h-3 w-3" />}
                      {status ? t('connectors.oauth.reconnect') : t('connectors.oauth.connect')}
                    </button>
                  )}

                  {/* Enabled toggle — fans out to every workspace */}
                  <button
                    type="button"
                    role="switch"
                    aria-checked={!!server.enabled}
                    aria-label={`${server.enabled ? 'Disable' : 'Enable'} ${server.name}`}
                    disabled={togglingName === server.name}
                    onClick={() => handleToggle(server, !server.enabled)}
                    className="relative inline-flex h-5 w-9 items-center rounded-full transition-colors"
                    style={{
                      backgroundColor: server.enabled ? 'var(--color-accent-primary)' : 'var(--color-border-muted)',
                    }}
                  >
                    <motion.span
                      className="inline-block h-4 w-4 rounded-full bg-white"
                      animate={{ x: server.enabled ? 18 : 2 }}
                      transition={SPRING_SNAPPY}
                    />
                  </button>

                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <button
                        type="button"
                        className="p-1.5 rounded transition-colors hover:bg-foreground/10"
                        style={{ color: 'var(--color-text-tertiary)' }}
                        aria-label={`Actions for ${server.name}`}
                      >
                        {refreshingName === server.name
                          ? <Loader size={16} className="text-current" />
                          : <MoreVertical className="h-4 w-4" />}
                      </button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end">
                      <DropdownMenuItem onSelect={() => { setEditing(server); setSubmitError(null); setModalOpen(true); }}>
                        <Pencil className="h-3.5 w-3.5 mr-2" />
                        {t('connectors.servers.edit')}
                      </DropdownMenuItem>
                      {oauthEligible && status === 'connected' && (
                        <DropdownMenuItem onSelect={() => handleRefreshSchemas(server.name)}>
                          <RefreshCw className="h-3.5 w-3.5 mr-2" />
                          {t('connectors.oauth.refreshSchemas')}
                        </DropdownMenuItem>
                      )}
                      {oauthEligible && status && status !== 'revoked' && (
                        <DropdownMenuItem onSelect={() => handleDisconnect(server.name)}>
                          <Link2Off className="h-3.5 w-3.5 mr-2" />
                          {t('connectors.oauth.disconnect')}
                        </DropdownMenuItem>
                      )}
                      <DropdownMenuItem onSelect={() => setDeletingName(server.name)} variant="destructive">
                        <Trash2 className="h-3.5 w-3.5 mr-2" />
                        {t('connectors.servers.delete')}
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {deletingName && (
        <div
          className="flex items-center justify-between gap-3 text-[0.6875rem] p-2 rounded"
          style={{ backgroundColor: 'var(--color-bg-card)', color: 'var(--color-text-secondary)', border: '1px solid var(--color-border-muted)' }}
        >
          <span className="min-w-0">
            {t('connectors.servers.deleteConfirm', { server: deletingName })}
          </span>
          <div className="flex items-center gap-1.5 flex-shrink-0">
            <button
              type="button"
              onClick={() => handleDelete(deletingName)}
              disabled={deleteMutation.isPending}
              className="px-2 py-1 rounded disabled:opacity-50"
              style={{ color: 'var(--color-loss)' }}
            >
              {deleteMutation.isPending ? t('common.loading') : t('connectors.servers.deleteConfirmYes')}
            </button>
            <button
              type="button"
              onClick={() => setDeletingName(null)}
              className="px-2 py-1 rounded hover:bg-foreground/10"
              style={{ color: 'var(--color-text-tertiary)' }}
            >
              {t('connectors.servers.deleteConfirmNo')}
            </button>
          </div>
        </div>
      )}

      {modalOpen && (
        <McpServerModal
          workspaceId=""
          secretNames={secretNames}
          initial={editing ? catalogToInitial(editing) : null}
          allowDiscover={false}
          onClose={() => { setModalOpen(false); setEditing(null); }}
          onSubmit={handleSubmit}
          createSecret={(body) => createSecretMutation.mutateAsync(body)}
          saving={createMutation.isPending || updateMutation.isPending}
          submitError={submitError}
        />
      )}

      {importOpen && (
        <McpImportModal
          onClose={() => setImportOpen(false)}
          onImport={(payload) => importMutation.mutateAsync(payload)}
          onImported={(createdNames) => {
            if (createdNames.length > 0) {
              toast({
                title: t('connectors.import.disabledNudgeTitle'),
                description: t('connectors.import.disabledNudgeDesc'),
              });
            }
          }}
        />
      )}
    </div>
  );
}

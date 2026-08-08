import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AnimatePresence } from 'framer-motion';
import {
  Download,
  Link2,
  Link2Off,
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
import { McpOauthPill } from '@/pages/ChatAgent/components/mcp/McpStatusPill';
import {
  ConfirmStrip,
  EnabledToggle,
  HeaderButton,
  KebabTrigger,
  ListEmpty,
  ListError,
  ListHeader,
  ListSkeleton,
  ServerNameLine,
  ServerRowShell,
  TagBadge,
} from '@/pages/ChatAgent/components/mcp/McpPrimitives';
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
 *
 * Row anatomy mirrors the workspace MCP tab (`McpServerRow`): identity line
 * (icon + name + transport badge), then the status line (OAuth pill + scope
 * text), then the description — same primitives, same rhythm.
 */

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
      const result = await toggleMutation.mutateAsync({ name: server.name, enabled });
      if (result.warnings?.length) {
        toast({
          title: t('connectors.servers.enabledWithWarnings'),
          description: result.warnings.join('\n'),
        });
      }
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
    return <ListSkeleton />;
  }

  return (
    <div className="flex flex-col gap-3">
      <ListHeader icon={Server} title={t('mcp.list.title')} count={servers.length} max={maxServers}>
        <HeaderButton
          variant="secondary"
          icon={Download}
          onClick={() => setImportOpen(true)}
          disabled={atCap}
          title={atCap ? t('mcp.list.atCap', { max: maxServers }) : t('mcp.list.importHint')}
        >
          {t('mcp.list.importJson')}
        </HeaderButton>
        <HeaderButton
          variant="primary"
          icon={Plus}
          onClick={() => { setEditing(null); setSubmitError(null); setModalOpen(true); }}
          disabled={atCap}
          title={atCap ? t('mcp.list.atCap', { max: maxServers }) : undefined}
        >
          {t('mcp.list.addServer')}
        </HeaderButton>
      </ListHeader>

      <p className="text-[0.6875rem]" style={{ color: 'var(--color-text-tertiary)' }}>
        {t('connectors.servers.inheritHint')}
      </p>

      {error ? (
        <ListError>
          {(error as { message?: string })?.message || t('mcp.list.loadFailed')}
        </ListError>
      ) : servers.length === 0 ? (
        <ListEmpty>{t('connectors.servers.empty')}</ListEmpty>
      ) : (
        <div className="flex flex-col gap-1.5">
          <AnimatePresence initial={false}>
            {servers.map((server) => {
              const oauthEligible = server.transport === 'http';
              const status = server.oauth_status ?? null;
              return (
                <ServerRowShell
                  key={server.name}
                  testid={`connector-row-${server.name}`}
                  main={
                    <>
                      <ServerNameLine icon={Server} name={server.name}>
                        <TagBadge>{server.transport}</TagBadge>
                      </ServerNameLine>

                      {/* Status line: OAuth pill + tool count + inheritance scope */}
                      <div className="flex items-center gap-2 flex-wrap">
                        {status && <McpOauthPill status={status} />}
                        {status === 'connected' && typeof server.tool_count === 'number' && server.tool_count > 0 && (
                          <span
                            className="text-[0.6875rem]"
                            style={{ color: 'var(--color-text-tertiary)' }}
                          >
                            {t('mcp.row.toolCount', { count: server.tool_count })}
                          </span>
                        )}
                        <span
                          className="text-[0.6875rem]"
                          style={{ color: server.enabled ? 'var(--color-text-secondary)' : 'var(--color-text-tertiary)' }}
                        >
                          {server.enabled
                            ? t('connectors.servers.enabledState')
                            : t('connectors.servers.disabledState')}
                        </span>
                      </div>

                      {server.description && (
                        <p className="text-[0.6875rem] line-clamp-2" style={{ color: 'var(--color-text-tertiary)' }}>
                          {server.description}
                        </p>
                      )}
                    </>
                  }
                  actions={
                    <>
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
                      <EnabledToggle
                        enabled={!!server.enabled}
                        name={server.name}
                        disabled={togglingName === server.name}
                        onToggle={() => handleToggle(server, !server.enabled)}
                      />

                      <DropdownMenu>
                        <DropdownMenuTrigger asChild>
                          <KebabTrigger
                            busy={refreshingName === server.name}
                            aria-label={t('mcp.row.actionsAria', { name: server.name })}
                          />
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end">
                          <DropdownMenuItem onSelect={() => { setEditing(server); setSubmitError(null); setModalOpen(true); }}>
                            <Pencil className="h-3.5 w-3.5 mr-2" />
                            {t('mcp.row.edit')}
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
                            {t('mcp.row.delete')}
                          </DropdownMenuItem>
                        </DropdownMenuContent>
                      </DropdownMenu>
                    </>
                  }
                />
              );
            })}
          </AnimatePresence>
        </div>
      )}

      {deletingName && (
        <ConfirmStrip
          message={t('connectors.servers.deleteConfirm', { server: deletingName })}
          confirmLabel={deleteMutation.isPending ? t('common.loading') : t('connectors.servers.deleteConfirmYes')}
          cancelLabel={t('connectors.servers.deleteConfirmNo')}
          pending={deleteMutation.isPending}
          onConfirm={() => handleDelete(deletingName)}
          onCancel={() => setDeletingName(null)}
        />
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

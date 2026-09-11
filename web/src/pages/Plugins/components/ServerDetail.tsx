import { useId, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader } from '@/components/ui/loader';
import {
  EnabledToggle,
  TagBadge,
} from '@/pages/ChatAgent/components/mcp/McpPrimitives';
import { oauthLabelKey } from '@/pages/ChatAgent/components/mcp/McpStatusPill';
import {
  useBrokerages,
  useBuiltinMcpServerTools,
  useMcpCatalogServerTools,
  useSetMcpServerBinding,
} from '@/hooks/useMcpServers';
import { brokerageArt, mcpServerArt } from '@/lib/brandArt';
import { brokerageForUrl, settledGrant, type Brokerage } from '../brokerages';
import { createDateFormatter } from '@/lib/format';
import {
  formatApiErrorDetail,
  type BuiltinMcpServer,
  type CatalogServer,
  type McpServerBindingPatch,
  type McpToolSummary,
  type WorkspaceScopedMcpServer,
} from '@/pages/ChatAgent/utils/api';
import {
  DetailField,
  DetailHeader,
  DetailOverlay,
  DetailSection,
} from './DetailOverlay';
import {
  BrokerFacts,
  CapabilityList,
  GroupedToolList,
} from './BrokerageDetailParts';
import { ConnectButton } from './OauthRowParts';
import { OrderCapabilityBadges } from './OrderCapabilityBadges';
import { PluginOriginBadge, PluginSuppressedBadge } from './PluginBadges';
import { ToolAccessSwitches, ToolBindingControl } from './ToolAccess';

/**
 * An MCP server's detail overlay, for every origin the Plugins page lists. The
 * union keeps each origin honest about what it knows: builtins carry no user
 * config, workspace rows are summaries (their editing stays on the workspace
 * tab), catalog rows carry the full config plus the host-side tool snapshot.
 *
 * A brokerage is the one origin that exists before its row does, which is why
 * its `server` is nullable and its identity comes from the registry instead.
 * It is otherwise an ordinary catalog row and shares every section below.
 */

/** The three origins the Connectors tab resolves; a brokerage is not one. */
export type McpServerDetailData =
  | { origin: 'builtin'; server: BuiltinMcpServer }
  | { origin: 'user'; server: CatalogServer }
  | { origin: 'workspace'; server: WorkspaceScopedMcpServer };

export type ServerDetailData =
  | McpServerDetailData
  | { origin: 'brokerage'; brokerage: Brokerage; server: CatalogServer | null };

const formatDate = createDateFormatter({ dateStyle: 'medium' });

export function ServerDetail({
  data,
  onClose,
  onToggle,
  toggling = false,
  workspaceName,
  onConnect,
  connecting = false,
}: {
  data: ServerDetailData;
  onClose: () => void;
  /** Absent = the surface has no toggle for this row (render read-only). */
  onToggle?: (enabled: boolean) => void;
  toggling?: boolean;
  /** Workspace rows: the display name of the owning workspace. */
  workspaceName?: string;
  /** Brokerages only: start or repair the connection. It is also the only way
   *  to change what the connection was granted, which is why it stays offered
   *  on one that is already connected. */
  onConnect?: () => void;
  connecting?: boolean;
}) {
  const { t } = useTranslation();
  const labelId = useId();
  const { origin } = data;
  const offer = data.origin === 'brokerage' ? data.brokerage : null;
  const catalog =
    data.origin === 'user' || data.origin === 'brokerage' ? data.server : null;
  // Two sources for the same section, because a server's tools are discovered
  // by whoever owns the server: the user's rows carry the snapshot taken when
  // they added or refreshed one, and a builtin's schemas are what this process
  // froze at startup. Only one of the two ever runs.
  const catalogTools = useMcpCatalogServerTools(catalog?.name ?? null);
  const builtinTools = useBuiltinMcpServerTools(
    data.origin === 'builtin' ? data.server.name : null,
  );
  const toolsQuery = origin === 'builtin' ? builtinTools : catalogTools;

  // Resolved the same way the row that opened this overlay resolves it, off the
  // address rather than the name: the two surfaces have to agree about which
  // vendor a row still points at, and a row edited elsewhere drops the mark
  // here for the same reason it drops it there. A brokerage with no row yet has
  // no address to resolve, and the offer it was opened from is the answer.
  const { data: brokerages } = useBrokerages();
  const resolved = brokerages ? brokerageForUrl(catalog?.url, brokerages) : null;
  const vendor = catalog ? resolved : (offer ?? resolved);
  const redirected = !!offer && !!catalog && vendor?.name !== offer.name;
  // Off the pill's own exhaustive table: a status added later is a compile
  // error there rather than a label that silently goes missing here.
  const oauthLabel = oauthLabelKey(catalog?.oauth_status);
  const granted = settledGrant(catalog?.granted_capabilities, catalog?.oauth_status);
  const groups = vendor?.capabilities ?? [];

  // One refusal at a time, pinned to the tool it was about: the server answers
  // 422 with the reason in words, and the words belong next to the select that
  // asked. A row-wide change carries no tool and lands under the switches.
  const setBinding = useSetMcpServerBinding();
  const [bindingError, setBindingError] = useState<{
    tool: string | null;
    message: string;
  } | null>(null);
  // Pinned the same way the refusal is, and for the same reason: the mutation
  // is shared but a request is about one tool, so a shared `isPending` would
  // freeze every other select on the server while one of them saves. A
  // row-wide change is the exception, since it rewrites all of them at once,
  // and rides here as `null`. A set rather than one slot because two tools can
  // be in flight at once: a single slot is cleared by whichever request lands
  // first, and the select it was still holding down comes back live mid-save.
  const [pending, setPending] = useState<ReadonlySet<string | null>>(new Set());
  const patchBinding = async (body: McpServerBindingPatch, tool: string | null = null) => {
    if (!catalog) return;
    setBindingError(null);
    setPending((prev) => new Set(prev).add(tool));
    try {
      await setBinding.mutateAsync({ name: catalog.name, body });
    } catch (err) {
      setBindingError({ tool, message: formatApiErrorDetail(err) });
    } finally {
      setPending((prev) => {
        const next = new Set(prev);
        next.delete(tool);
        return next;
      });
    }
  };
  const bindingControl = catalog
    ? (tool: McpToolSummary) => (
        <ToolBindingControl
          tool={tool}
          catalog={catalog}
          busy={pending.has(null) || pending.has(tool.name)}
          error={bindingError?.tool === tool.name ? bindingError.message : null}
          onPatch={(body) => patchBinding(body, tool.name)}
        />
      )
    : null;

  // A brokerage wears the vendor's label until its row is pointed elsewhere,
  // exactly as its row does; every other origin is its own name and always was.
  const name =
    data.origin === 'brokerage'
      ? redirected
        ? (data.server?.name ?? data.brokerage.name)
        : data.brokerage.label
      : data.server.name;
  // `http` is what enabling a brokerage would create, and the only transport an
  // OAuth connect is allowed on; past that first write the row owns the answer.
  const transport =
    data.origin === 'brokerage'
      ? (data.server?.transport ?? 'http')
      : data.server.transport;
  const description =
    data.origin === 'brokerage'
      ? redirected
        ? data.server?.description
        : data.server?.description || data.brokerage.description
      : data.server.description;
  const enabled =
    data.origin === 'brokerage'
      ? (data.server?.enabled ?? false)
      : (data.server.enabled ?? false);

  return (
    <DetailOverlay
      labelId={labelId}
      onClose={onClose}
      footer={
        origin === 'brokerage' &&
        onConnect && (
          <div className="flex items-center justify-end">
            {/* Offered on a live connection too, and not only a broken one:
                reconnecting is the only way to change what was granted, so the
                control that changes it is the one that made it. The sentence
                saying so sits with the list it would change, not here. */}
            <ConnectButton
              status={catalog?.oauth_status ?? null}
              connecting={connecting}
              vendor={vendor}
              rowKey={`brokerage-detail-${name}`}
              emphasis={catalog?.oauth_status ? 'quiet' : 'loud'}
              onClick={onConnect}
            />
          </div>
        )
      }
      header={
        <DetailHeader
          name={name}
          labelId={labelId}
          kind="server"
          kindLabel={t(
            origin === 'brokerage'
              ? 'plugins.detail.kindBrokerage'
              : 'plugins.detail.kindServer',
          )}
          art={brokerageArt(vendor) ?? (catalog ? mcpServerArt(catalog) : undefined)}
          meta={
            <>
              <span>{transport}</span>
              {origin === 'builtin' && <span>{t('plugins.mcp.platformBadge')}</span>}
              {origin === 'workspace' && (
                <span>
                  {t('plugins.scope.inWorkspace', {
                    name: workspaceName ?? t('plugins.scope.unknownWorkspace'),
                  })}
                </span>
              )}
              {origin === 'brokerage' && !catalog && (
                <span>{t('plugins.brokerages.notAdded')}</span>
              )}
              <PluginOriginBadge plugin={catalog?.plugin_name} variant="prose" />
              <PluginSuppressedBadge row={catalog} variant="prose" />
              {oauthLabel && <span>{t(oauthLabel)}</span>}
              {/* The one thing a broker is asked first, in the same words the
                  consent toggle uses for it. */}
              {origin === 'brokerage' && (
                <OrderCapabilityBadges vendor={vendor} granted={granted} />
              )}
            </>
          }
          controls={
            onToggle && (
              // Stays live while the owning plugin is off: the row keeps its
              // own `enabled`, and that flag decides whether it comes back
              // when the plugin does. The badge above says why it is not
              // delivered right now; disabling the switch would strand the
              // user with no way to exclude one component of a plugin they
              // are about to turn back on.
              <EnabledToggle
                enabled={enabled}
                name={name}
                disabled={toggling}
                onToggle={() => onToggle(!enabled)}
              />
            )
          }
        />
      }
    >
      {description && (
        <p className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
          {description}
        </p>
      )}

      {/* Before the tools, because it decides which of them are reachable, and
          because it is what someone opened a broker's detail to find out. */}
      {origin === 'brokerage' && groups.length > 0 && (
        <DetailSection title={t('plugins.brokerages.detail.capabilities')}>
          <CapabilityList groups={groups} granted={granted} />
        </DetailSection>
      )}

      {/* The row-wide switches are a broker's alone: only its tools belong to
          capability groups, and the two switches speak about one of them. An
          ordinary server has per-tool controls in the list below and nothing
          row-wide to say here. */}
      {origin === 'brokerage' && catalog && (
        <DetailSection title={t('plugins.detail.toolAccess')}>
          <div className="flex flex-col gap-3">
            <ToolAccessSwitches
              catalog={catalog}
              busy={pending.size > 0}
              onPatch={(body) => patchBinding(body)}
            />
            {bindingError && bindingError.tool === null && (
              <p role="alert" className="text-[0.6875rem]" style={{ color: 'var(--color-loss)' }}>
                {bindingError.message}
              </p>
            )}
            <p className="text-[0.6875rem]" style={{ color: 'var(--color-text-quaternary)' }}>
              {t('plugins.detail.toolAccessNote')}
            </p>
          </div>
        </DetailSection>
      )}

      {(origin === 'user' || origin === 'builtin' || !!catalog) && (
        <DetailSection
          title={t('plugins.detail.tools')}
          count={toolsQuery.data?.tools.length}
        >
          {toolsQuery.isLoading ? (
            <div className="flex items-center gap-2 py-3">
              <Loader size={14} className="text-current" />
              <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                {t('common.loading')}
              </span>
            </div>
          ) : toolsQuery.isError ? (
            // Distinct from the empty case below. Both used to read
            // "no tools discovered yet", which tells the user to wait when
            // the truth is that the request failed and wants a retry.
            <p className="text-xs" style={{ color: 'var(--color-loss)' }}>
              {t('plugins.detail.toolsFailed')}
            </p>
          ) : builtinTools.data?.connected === false ? (
            // Third state, distinct from both above: the request succeeded and
            // this worker simply has no snapshot, because its startup connect
            // failed and a frozen registry is never repaired. Saying "no tools"
            // here would report one process's gap as the server's shape.
            <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              {t('plugins.detail.toolsUnavailable')}
            </p>
          ) : !toolsQuery.data || toolsQuery.data.tools.length === 0 ? (
            <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              {t('plugins.detail.toolsEmpty')}
            </p>
          ) : (
            <div className="flex flex-col gap-2">
              {/* A brokerage publishes one flat list of up to 88 tools, and
                  reading it top to bottom answers nothing. Under the consent
                  group that reaches each one, the same list says which of them
                  the agent can actually call. */}
              {origin === 'brokerage' && groups.length > 0 ? (
                <GroupedToolList
                  groups={groups}
                  granted={granted}
                  tools={toolsQuery.data.tools}
                  renderControl={bindingControl ?? undefined}
                />
              ) : (
                toolsQuery.data.tools.map((tool) => (
                  <div key={tool.name} className="flex items-start justify-between gap-2">
                    <div className="flex flex-col gap-0.5 min-w-0">
                      <span
                        className="text-[0.6875rem] font-medium break-all"
                        style={{
                          color: 'var(--color-text-secondary)',
                          fontFamily: "'JetBrains Mono', 'Menlo', monospace",
                        }}
                      >
                        {tool.name}
                      </span>
                      {tool.description && (
                        <span
                          className="text-[0.6875rem] line-clamp-2"
                          style={{ color: 'var(--color-text-tertiary)' }}
                        >
                          {tool.description}
                        </span>
                      )}
                    </div>
                    {bindingControl?.(tool)}
                  </div>
                ))
              )}
              {toolsQuery.data.discovered_at && (
                <span
                  className="text-[0.6875rem]"
                  style={{ color: 'var(--color-text-quaternary)' }}
                >
                  {t('plugins.detail.discovered', {
                    date: formatDate(new Date(toolsQuery.data.discovered_at)),
                  })}
                </span>
              )}
            </div>
          )}
        </DetailSection>
      )}

      {vendor && origin === 'brokerage' && (
        <DetailSection title={t('plugins.brokerages.detail.broker')}>
          <BrokerFacts vendor={vendor} rowUrl={catalog?.url} />
        </DetailSection>
      )}

      {/* Nothing to configure until the row exists: the section above already
          named the address, and a lone Transport line is not a configuration. */}
      {!(origin === 'brokerage' && !catalog) && (
        <DetailSection title={t('plugins.detail.config')}>
          <div className="flex flex-col gap-1.5">
            <DetailField label={t('plugins.detail.transport')}>
              {transport}
            </DetailField>
            {catalog?.url && (
              <DetailField label={t('plugins.detail.url')}>{catalog.url}</DetailField>
            )}
            {catalog?.command && (
              <DetailField label={t('plugins.detail.command')}>
                {[catalog.command, ...(catalog.args ?? [])].join(' ')}
              </DetailField>
            )}
            {catalog && catalog.env_refs.length > 0 && (
              <DetailField label={t('plugins.detail.envVars')}>
                <span className="inline-flex items-center gap-1 flex-wrap">
                  {catalog.env_refs.map((ref) => (
                    <TagBadge key={ref} soft>
                      {ref}
                    </TagBadge>
                  ))}
                </span>
              </DetailField>
            )}
            {catalog && catalog.header_refs.length > 0 && (
              <DetailField label={t('plugins.detail.headers')}>
                <span className="inline-flex items-center gap-1 flex-wrap">
                  {catalog.header_refs.map((ref) => (
                    <TagBadge key={ref} soft>
                      {ref}
                    </TagBadge>
                  ))}
                </span>
              </DetailField>
            )}
          </div>
        </DetailSection>
      )}

      {catalog && (catalog.created_at || catalog.updated_at) && (
        <DetailSection title={t('plugins.detail.info')}>
          <div className="flex flex-col gap-1.5">
            {catalog.created_at && (
              <DetailField label={t('plugins.detail.created')}>
                {formatDate(new Date(catalog.created_at))}
              </DetailField>
            )}
            {catalog.updated_at && (
              <DetailField label={t('plugins.detail.updated')}>
                {formatDate(new Date(catalog.updated_at))}
              </DetailField>
            )}
          </div>
        </DetailSection>
      )}
    </DetailOverlay>
  );
}

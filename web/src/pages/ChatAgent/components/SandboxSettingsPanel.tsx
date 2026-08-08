import React, { useState, useEffect, useMemo, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import {
  X, Cpu, MemoryStick, HardDrive, MonitorCog, Play, Square,
  Package, Search, RefreshCw, ChevronDown, ChevronRight,
  Server, BookOpen, Archive,
} from 'lucide-react';
import { Loader } from '@/components/ui/loader';
import {
  getSandboxStats, installSandboxPackages, refreshWorkspace,
  getVaultSecrets, createVaultSecret, updateVaultSecret, deleteVaultSecret, revealVaultSecret,
  getVaultBlueprints, type VaultBlueprint,
} from '../utils/api';
import { api } from '@/api/client';
import { McpTab } from './mcp/McpTab';
import { SecretsManager } from './vault/SecretsManager';

interface SandboxSettingsPanelProps {
  onClose: () => void;
  workspaceId: string;
}

interface SandboxPackage {
  name: string;
  version: string;
}

interface DirBreakdownEntry {
  path: string;
  size: string;
}

interface DiskUsage {
  used: string;
  available: string;
  total: string;
  use_percent: string;
}

interface SandboxSkill {
  name: string;
  description?: string;
}

interface SandboxStats {
  /** Display vocabulary, not the backend RuntimeState enum: 'running' is canonical
   *  across providers; anything outside TERMINAL_STATES is treated as in-progress. */
  state: string | null;
  /** Null, not absent, for a workspace that has no sandbox yet. */
  sandbox_id?: string | null;
  /** Which provider answered, e.g. 'daytona' | 'docker'. Null, not absent, when unresolved. */
  provider?: string | null;
  created_at?: string;
  auto_stop_interval?: number;
  resources: {
    cpu?: number;
    memory?: number;
    disk?: number;
    gpu?: number;
  };
  disk_usage?: DiskUsage;
  directory_breakdown?: DirBreakdownEntry[];
  packages?: SandboxPackage[];
  default_packages?: string[];
  mcp_servers?: string[];
  skills?: SandboxSkill[];
}

interface InstallResult {
  success: boolean;
  output: string;
  error?: string;
  installed: string[];
}

interface RefreshResult {
  status: string;
  message?: string;
  refreshed_tools?: boolean;
  skills_uploaded?: boolean;
  servers?: string[];
}

/**
 * SandboxSettingsContent -- sandbox settings tabs and content, usable inline or in a modal.
 */
export function SandboxSettingsContent({ workspaceId }: { workspaceId: string }) {
  const [activeTab, setActiveTab] = useState('overview');
  const [stats, setStats] = useState<SandboxStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Package install state
  const [installInput, setInstallInput] = useState('');
  const [installing, setInstalling] = useState(false);
  const [installResult, setInstallResult] = useState<InstallResult | null>(null);

  // Package search
  const [pkgSearch, setPkgSearch] = useState('');

  // Storage expand
  const [showDirBreakdown, setShowDirBreakdown] = useState(false);

  // Tools refresh
  const [refreshing, setRefreshing] = useState(false);
  const [refreshResult, setRefreshResult] = useState<RefreshResult | null>(null);

  // Start/stop
  const [actionLoading, setActionLoading] = useState(false);

  // Only the newest stats request may commit. Refresh is deliberately never
  // disabled, so a slow full-path read (~15s of probes) can still be in flight
  // when a faster post-action read lands — without this the older response wins
  // by arriving last and resurrects a stopped sandbox as running.
  const statsRequestRef = useRef(0);

  // Vault deep-link: an MCP "Set up NAME" affordance switches to the Vault tab
  // and prefills the add form with that secret name.
  const [vaultPrefillSecret, setVaultPrefillSecret] = useState<string | null>(null);

  useEffect(() => {
    if (!workspaceId) return;
    // Drop the outgoing workspace's stats before reading the new one. A refresh
    // now keeps the panel on screen rather than blanking it, so without this a
    // workspace switch would render the old sandbox under the new id.
    setStats(null);
    loadStats();
  }, [workspaceId]);

  async function loadStats() {
    const requestId = ++statsRequestRef.current;
    setLoading(true);
    setError(null);
    try {
      const data = await getSandboxStats(workspaceId);
      if (requestId !== statsRequestRef.current) return;
      setStats(data);
    } catch (err: any) { // TODO: type properly
      if (requestId !== statsRequestRef.current) return;
      setError(err?.response?.data?.detail || err.message || 'Failed to load sandbox stats');
    } finally {
      if (requestId === statsRequestRef.current) setLoading(false);
    }
  }

  async function handleStartStop(action: string) {
    setActionLoading(true);
    try {
      await api.post(`/api/v1/workspaces/${workspaceId}/${action}`);
      await loadStats();
    } catch (err: any) { // TODO: type properly
      setError(err?.response?.data?.detail || `Failed to ${action} workspace`);
    } finally {
      setActionLoading(false);
    }
  }

  async function handleInstall() {
    const packages = installInput.split(/[\s,]+/).filter(Boolean);
    if (!packages.length) return;
    setInstalling(true);
    setInstallResult(null);
    try {
      const result = await installSandboxPackages(workspaceId, packages);
      setInstallResult(result);
      if (result.success) {
        setInstallInput('');
        // Refresh stats to show new packages
        loadStats();
      }
    } catch (err: any) { // TODO: type properly
      setInstallResult({
        success: false,
        output: '',
        error: err?.response?.data?.detail || err.message,
        installed: [],
      });
    } finally {
      setInstalling(false);
    }
  }

  async function handleRefresh() {
    setRefreshing(true);
    setRefreshResult(null);
    try {
      const result = await refreshWorkspace(workspaceId);
      setRefreshResult(result);
      // Reload stats to get updated MCP list
      loadStats();
    } catch (err: any) { // TODO: type properly
      setRefreshResult({ status: 'error', message: err?.response?.data?.detail || err.message });
    } finally {
      setRefreshing(false);
    }
  }

  // Filter packages by search
  const filteredPackages = useMemo(() => {
    if (!stats?.packages) return [];
    if (!pkgSearch.trim()) return stats.packages;
    const q = pkgSearch.toLowerCase();
    return stats.packages.filter(p => p.name.toLowerCase().includes(q));
  }, [stats?.packages, pkgSearch]);

  const defaultPkgSet = useMemo(
    () => new Set((stats?.default_packages || []).map(p => p.split(/[<>=!~]/)[0].toLowerCase())),
    [stats?.default_packages],
  );

  const tabs = [
    { key: 'overview', label: 'Overview' },
    { key: 'vault', label: 'Vault' },
    { key: 'mcp', label: 'MCP' },
    { key: 'storage', label: 'Storage' },
    { key: 'packages', label: 'Packages' },
    { key: 'tools', label: 'Tools & Skills' },
  ];

  function openVaultTab(prefillSecretName?: string) {
    setVaultPrefillSecret(prefillSecretName ?? null);
    setActiveTab('vault');
  }

  // Canonical value only. Provider synonyms are the API's job to translate — see
  // _DISPLAY_STATE_SYNONYMS server-side.
  const isRunning = stats?.state === 'running';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* Tabs */}
      <div className="flex flex-wrap gap-1 mb-4 border-b" style={{ borderColor: 'var(--color-border-muted)' }}>
        {tabs.map(t => (
          <button
            key={t.key}
            type="button"
            onClick={() => setActiveTab(t.key)}
            className="px-3 py-2 text-sm font-medium"
            style={{
              color: activeTab === t.key ? 'var(--color-text-primary)' : 'var(--color-text-tertiary)',
              borderBottom: activeTab === t.key ? '2px solid var(--color-accent-primary)' : '2px solid transparent',
            }}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Content */}
      <div style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
      {/* Skeleton only before the first load. Refresh reads through the same
          path, and blanking the panel would discard the status the user is
          watching — and unmount the button they just pressed. */}
      {loading && !stats ? (
        <LoadingSkeleton />
      ) : error ? (
        <ErrorState message={error} onRetry={loadStats} />
      ) : (
        <>
          {activeTab === 'overview' && (
            <OverviewTab
              stats={stats!}
              isRunning={isRunning!}
              actionLoading={actionLoading}
              refreshing={loading}
              onStartStop={handleStartStop}
              onRefresh={loadStats}
            />
          )}
          {activeTab === 'vault' && (
            <SecretsTab
              workspaceId={workspaceId}
              prefillSecretName={vaultPrefillSecret}
              onPrefillConsumed={() => setVaultPrefillSecret(null)}
            />
          )}
          {activeTab === 'mcp' && (
            <McpTab workspaceId={workspaceId} onOpenVaultTab={openVaultTab} />
          )}
          {activeTab === 'storage' && (
            isRunning ? (
              <StorageTab
                stats={stats!}
                showDirBreakdown={showDirBreakdown}
                onToggleBreakdown={() => setShowDirBreakdown(!showDirBreakdown)}
              />
            ) : (
              <OfflineTabPlaceholder tabName="storage" />
            )
          )}
          {activeTab === 'packages' && (
            isRunning ? (
              <PackagesTab
                filteredPackages={filteredPackages}
                defaultPkgSet={defaultPkgSet}
                pkgSearch={pkgSearch}
                onSearchChange={setPkgSearch}
                installInput={installInput}
                onInstallInputChange={setInstallInput}
                installing={installing}
                installResult={installResult}
                onInstall={handleInstall}
              />
            ) : (
              <OfflineTabPlaceholder tabName="packages" />
            )
          )}
          {activeTab === 'tools' && (
            isRunning ? (
              <ToolsTab
                stats={stats!}
                refreshing={refreshing}
                refreshResult={refreshResult}
                onRefresh={handleRefresh}
              />
            ) : (
              <OfflineTabPlaceholder tabName="tools & skills" />
            )
          )}
        </>
      )}
      </div>
    </div>
  );
}

/**
 * SandboxSettingsPanel -- full-screen overlay showing sandbox details.
 */
export default function SandboxSettingsPanel({ onClose, workspaceId }: SandboxSettingsPanelProps) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center"
      style={{ backgroundColor: 'var(--color-bg-overlay-strong)' }}
      onClick={onClose}
    >
      <div
        className="relative w-full max-w-2xl rounded-lg p-4 sm:p-6"
        style={{
          backgroundColor: 'var(--color-bg-elevated)',
          border: '1px solid var(--color-border-muted)',
          height: 'min(80vh, 650px)',
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Close button */}
        <button
          onClick={onClose}
          className="absolute top-4 right-4 p-1 rounded-full transition-colors hover:bg-foreground/10"
          style={{ color: 'var(--color-text-primary)' }}
        >
          <X className="h-5 w-5" />
        </button>

        {/* Title */}
        <h2 className="text-xl font-semibold mb-6" style={{ color: 'var(--color-text-primary)' }}>
          Sandbox Settings
        </h2>

        <SandboxSettingsContent workspaceId={workspaceId} />
      </div>
    </div>
  );
}


// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function LoadingSkeleton() {
  return (
    <div className="flex flex-col gap-4">
      {[1, 2, 3, 4].map(i => (
        <div
          key={i}
          className="h-16 rounded-lg animate-pulse"
          style={{ backgroundColor: 'var(--color-bg-card)' }}
        />
      ))}
    </div>
  );
}

interface ErrorStateProps {
  message: string;
  onRetry: () => void;
}

function ErrorState({ message, onRetry }: ErrorStateProps) {
  return (
    <div className="flex flex-col items-center gap-4 py-8">
      <p className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>{message}</p>
      <button
        onClick={onRetry}
        className="px-4 py-2 text-sm rounded-md transition-colors hover:bg-foreground/10"
        style={{ color: 'var(--color-text-primary)', border: '1px solid var(--color-border-elevated)' }}
      >
        Retry
      </button>
    </div>
  );
}


interface OfflineTabPlaceholderProps {
  tabName: string;
}

function OfflineTabPlaceholder({ tabName }: OfflineTabPlaceholderProps) {
  return (
    <div className="py-8 text-center text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
      Start the workspace to view {tabName}
    </div>
  );
}


// ---------------------------------------------------------------------------
// Overview Tab
// ---------------------------------------------------------------------------

// States where the sandbox has settled. Deliberately an allowlist of *terminal*
// values rather than of in-progress ones: the provider has ~23 states and keeps
// adding them, so anything unrecognized must fail safe to "in progress" (spinner,
// actions disabled) instead of rendering as a settled failure with a live Start.
// Provider synonyms are deliberately absent: the API canonicalizes them, and
// compensating here too would let the two vocabularies drift apart silently.
const TERMINAL_STATES = new Set([
  'running',
  'unknown',
  'stopped',
  'archived',
  'error',
  'paused',
  'destroyed',
  'build_failed',
  'deleted',
]);

// Wire values are provider identifiers, not user copy. Unmapped values must not
// reach the screen: daytona's SDK coerces anything it doesn't recognize to
// 'unknown_default_open_api', and a bare capitalize would render that verbatim.
const STATE_LABELS: Record<string, string> = {
  running: 'Running',
  stopped: 'Stopped',
  starting: 'Starting',
  stopping: 'Stopping',
  archiving: 'Archiving',
  archived: 'Archived',
  restoring: 'Restoring',
  resizing: 'Resizing',
  creating: 'Creating',
  destroying: 'Destroying',
  destroyed: 'Destroyed',
  pausing: 'Pausing',
  paused: 'Paused',
  resuming: 'Resuming',
  snapshotting: 'Snapshotting',
  forking: 'Forking',
  error: 'Error',
  deleted: 'Deleted',
  build_failed: 'Build failed',
  pending_build: 'Pending build',
  building_snapshot: 'Building snapshot',
  pulling_snapshot: 'Pulling snapshot',
  unknown: 'Unknown',
};

interface OverviewTabProps {
  stats: SandboxStats;
  isRunning: boolean;
  actionLoading: boolean;
  refreshing: boolean;
  onStartStop: (action: string) => void;
  onRefresh: () => void;
}

function OverviewTab({ stats, isRunning, actionLoading, refreshing, onStartStop, onRefresh }: OverviewTabProps) {
  const isTransitioning =
    actionLoading || (!!stats.state && !TERMINAL_STATES.has(stats.state));
  const stateLabel = stats.state
    ? (STATE_LABELS[stats.state] ?? 'Updating')
    : 'Unknown';
  const resourceCards = [
    { icon: Cpu, label: 'CPU', value: stats.resources.cpu != null ? `${stats.resources.cpu} vCPU` : '---' },
    { icon: MemoryStick, label: 'Memory', value: stats.resources.memory != null ? `${stats.resources.memory} GiB` : '---' },
    { icon: HardDrive, label: 'Disk', value: stats.resources.disk != null ? `${stats.resources.disk} GiB` : '---' },
    { icon: MonitorCog, label: 'GPU', value: stats.resources.gpu != null ? `${stats.resources.gpu} GPU` : '---' },
  ];

  return (
    <div className="flex flex-col gap-5">
      {/* Resource cards -- 2x2 grid */}
      <div className="grid grid-cols-2 gap-3">
        {resourceCards.map(({ icon: Icon, label, value }) => (
          <div
            key={label}
            className="flex items-center gap-3 p-3 rounded-lg"
            style={{ backgroundColor: 'var(--color-bg-card)', border: '1px solid var(--color-border-muted)' }}
          >
            <Icon className="h-5 w-5 flex-shrink-0" style={{ color: 'var(--color-accent-primary)' }} />
            <div>
              <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>{label}</div>
              <div className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>{value}</div>
            </div>
          </div>
        ))}
      </div>

      {/* Status + metadata */}
      <div
        className="flex items-center justify-between p-3 rounded-lg"
        style={{ backgroundColor: 'var(--color-bg-card)', border: '1px solid var(--color-border-muted)' }}
      >
        <div className="flex items-center gap-3" role="status" aria-live="polite">
          {isTransitioning ? (
            <span aria-hidden="true" className="flex-shrink-0">
              <Loader size={14} className="text-[color:var(--color-text-tertiary)]" />
            </span>
          ) : (
            <div
              aria-hidden="true"
              className="w-2.5 h-2.5 rounded-full flex-shrink-0"
              style={{ backgroundColor: isRunning ? 'var(--color-profit)' : 'var(--color-loss)' }}
            />
          )}
          <div>
            <div className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
              {isTransitioning
                ? (actionLoading ? 'Updating...' : `${stateLabel}...`)
                : stateLabel}
            </div>
            {stats.created_at && (
              <div className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>
                Created {new Date(stats.created_at).toLocaleDateString()}
              </div>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          {stats.auto_stop_interval != null && (
            <span className="text-xs px-2 py-1 rounded" style={{ color: 'var(--color-text-tertiary)', backgroundColor: 'var(--color-bg-card)' }}>
              {/* 0 disables auto-stop entirely, so rendering "0m" states the opposite */}
              {stats.auto_stop_interval === 0
                ? 'Always on'
                : `Auto-stop: ${stats.auto_stop_interval}m`}
            </span>
          )}
          {/* Never disabled. In a transitional state every other control here is,
              and nothing polls — without this the panel has no way to advance. */}
          <button
            onClick={onRefresh}
            aria-label="Refresh sandbox status"
            title="Refresh status"
            className="flex items-center gap-1.5 px-2 py-1.5 text-xs rounded-md transition-colors hover:bg-foreground/10"
            style={{ color: 'var(--color-text-tertiary)', border: '1px solid var(--color-border-muted)' }}
          >
            {refreshing
              ? (
                <span aria-hidden="true" className="flex-shrink-0">
                  <Loader size={12} className="text-current" />
                </span>
              )
              : <RefreshCw className="h-3 w-3" aria-hidden="true" />}
          </button>
          {!isRunning && stats.state === 'stopped' && (
            <button
              onClick={() => onStartStop('archive')}
              disabled={isTransitioning}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-md transition-colors hover:bg-foreground/10 disabled:opacity-50"
              style={{ color: 'var(--color-text-tertiary)', border: '1px solid var(--color-border-muted)' }}
            >
              <Archive className="h-3 w-3" />
              Archive
            </button>
          )}
          {isRunning ? (
            <button
              onClick={() => onStartStop('stop')}
              disabled={isTransitioning}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-md transition-colors hover:bg-foreground/10 disabled:opacity-50"
              style={{ color: 'var(--color-loss)', border: '1px solid var(--color-border-loss)' }}
            >
              <Square className="h-3 w-3" />
              Stop
            </button>
          ) : (
            <button
              onClick={() => onStartStop('start')}
              disabled={isTransitioning}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-md transition-colors hover:bg-foreground/10 disabled:opacity-50"
              style={{ color: 'var(--color-profit)', border: '1px solid var(--color-profit-border)' }}
            >
              <Play className="h-3 w-3" />
              Start
            </button>
          )}
        </div>
      </div>

      {/* Sandbox ID */}
      {stats.sandbox_id && (
        <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
          Sandbox ID: <span className="font-mono">{stats.sandbox_id}</span>
        </div>
      )}
    </div>
  );
}


// ---------------------------------------------------------------------------
// Storage Tab
// ---------------------------------------------------------------------------

interface StorageTabProps {
  stats: SandboxStats;
  showDirBreakdown: boolean;
  onToggleBreakdown: () => void;
}

function StorageTab({ stats, showDirBreakdown, onToggleBreakdown }: StorageTabProps) {
  const disk = stats.disk_usage;

  if (!disk) {
    return (
      <div className="py-8 text-center text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
        Disk usage information unavailable
      </div>
    );
  }

  // Parse use_percent for the progress bar
  const pct = parseInt(disk.use_percent, 10) || 0;

  return (
    <div className="flex flex-col gap-5">
      {/* Usage bar. Docker sets no size quota, so df(1) inside the container reports
          the host filesystem — showing it as the sandbox's disk would be a wrong number. */}
      {stats.provider === 'docker' ? (
        <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
          Local sandboxes run without a disk quota, so total usage would describe the
          host filesystem rather than this sandbox. The per-directory sizes below are
          sandbox-scoped.
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          <div className="flex justify-between text-sm" style={{ color: 'var(--color-text-primary)' }}>
            <span>{disk.used} used</span>
            <span>{disk.available} available</span>
          </div>
          <div className="h-3 rounded-full overflow-hidden" style={{ backgroundColor: 'var(--color-bg-card)' }}>
            <div
              className="h-full rounded-full transition-all"
              style={{
                width: `${pct}%`,
                backgroundColor: pct > 80 ? 'var(--color-loss)' : 'var(--color-accent-primary)',
              }}
            />
          </div>
          <div className="flex justify-between text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            <span>{disk.use_percent} used</span>
            <span>{disk.total} total</span>
          </div>
        </div>
      )}

      {/* Directory breakdown toggle */}
      {stats.directory_breakdown && stats.directory_breakdown.length > 0 && (
        <div>
          <button
            onClick={onToggleBreakdown}
            className="flex items-center gap-1.5 text-sm transition-colors hover:opacity-80"
            style={{ color: 'var(--color-text-secondary)' }}
          >
            {showDirBreakdown ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
            Details ({stats.directory_breakdown.length} directories)
          </button>

          {showDirBreakdown && (
            <div className="mt-3 flex flex-col gap-1">
              {stats.directory_breakdown.map((d) => (
                <div
                  key={d.path}
                  className="flex justify-between py-1.5 px-3 rounded text-sm"
                  style={{ backgroundColor: 'var(--color-bg-card)' }}
                >
                  <span className="font-mono truncate" style={{ color: 'var(--color-text-primary)' }}>{d.path}/</span>
                  <span className="flex-shrink-0 ml-4" style={{ color: 'var(--color-text-tertiary)' }}>{d.size}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}


// ---------------------------------------------------------------------------
// Packages Tab
// ---------------------------------------------------------------------------

interface PackagesTabProps {
  filteredPackages: SandboxPackage[];
  defaultPkgSet: Set<string>;
  pkgSearch: string;
  onSearchChange: (value: string) => void;
  installInput: string;
  onInstallInputChange: (value: string) => void;
  installing: boolean;
  installResult: InstallResult | null;
  onInstall: () => void;
}

function PackagesTab({
  filteredPackages, defaultPkgSet, pkgSearch, onSearchChange,
  installInput, onInstallInputChange, installing, installResult, onInstall,
}: PackagesTabProps) {
  return (
    <div className="flex flex-col gap-4">
      {/* Search */}
      <div className="relative">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4" style={{ color: 'var(--color-text-tertiary)' }} />
        <input
          type="text"
          value={pkgSearch}
          onChange={e => onSearchChange(e.target.value)}
          placeholder="Filter packages..."
          className="w-full pl-9 pr-3 py-2 text-sm rounded-md bg-transparent outline-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-[color:var(--color-accent-primary)]"
          style={{
            color: 'var(--color-text-primary)',
            border: '1px solid var(--color-border-muted)',
          }}
        />
      </div>

      {/* Package list */}
      <div
        className="flex flex-col gap-0.5 overflow-y-auto"
        style={{ maxHeight: '320px' }}
      >
        {filteredPackages.length === 0 ? (
          <div className="py-6 text-center text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
            {pkgSearch ? 'No matching packages' : 'No packages installed'}
          </div>
        ) : (
          filteredPackages.map(p => {
            const isDefault = defaultPkgSet.has(p.name.toLowerCase());
            return (
              <div
                key={p.name}
                className="flex justify-between items-center py-1.5 px-3 rounded text-sm"
                style={{ backgroundColor: 'var(--color-bg-card)' }}
              >
                <div className="flex items-center gap-2">
                  <span style={{ color: isDefault ? 'var(--color-text-tertiary)' : 'var(--color-text-primary)' }}>
                    {p.name}
                  </span>
                  {isDefault && (
                    <span
                      className="text-[0.625rem] px-1.5 py-0.5 rounded"
                      style={{ color: 'var(--color-text-tertiary)', backgroundColor: 'var(--color-bg-card)' }}
                    >
                      default
                    </span>
                  )}
                </div>
                <span className="font-mono text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                  {p.version}
                </span>
              </div>
            );
          })
        )}
      </div>

      {/* Install section */}
      <div
        className="flex flex-col gap-2 pt-3 border-t"
        style={{ borderColor: 'var(--color-border-muted)' }}
      >
        <div className="flex gap-2">
          <input
            type="text"
            value={installInput}
            onChange={e => onInstallInputChange(e.target.value)}
            placeholder="Package names (e.g. torch transformers>=4.0)"
            onKeyDown={e => e.key === 'Enter' && !installing && onInstall()}
            className="flex-1 px-3 py-2 text-sm rounded-md bg-transparent outline-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-[color:var(--color-accent-primary)]"
            style={{
              color: 'var(--color-text-primary)',
              border: '1px solid var(--color-border-muted)',
            }}
          />
          <button
            onClick={onInstall}
            disabled={installing || !installInput.trim()}
            className="flex items-center gap-1.5 px-4 py-2 text-sm rounded-md transition-colors disabled:opacity-50"
            style={{
              color: 'var(--color-btn-primary-text)',
              backgroundColor: 'var(--color-btn-primary-bg)',
            }}
          >
            {installing ? <Loader size={14} className="text-current" /> : <Package className="h-3.5 w-3.5" />}
            Install
          </button>
        </div>

        {/* Install result */}
        {installResult && (
          <div
            className="text-xs p-2 rounded font-mono whitespace-pre-wrap max-h-32 overflow-y-auto"
            style={{
              backgroundColor: 'var(--color-bg-card)',
              color: installResult.success ? 'var(--color-text-secondary)' : 'var(--color-loss)',
            }}
          >
            {installResult.error || installResult.output || 'Done'}
          </div>
        )}
      </div>
    </div>
  );
}


// ---------------------------------------------------------------------------
// Tools & Skills Tab
// ---------------------------------------------------------------------------

interface ToolsTabProps {
  stats: SandboxStats;
  refreshing: boolean;
  refreshResult: RefreshResult | null;
  onRefresh: () => void;
}

function ToolsTab({ stats, refreshing, refreshResult, onRefresh }: ToolsTabProps) {
  return (
    <div className="flex flex-col gap-5">
      {/* MCP Servers list */}
      <div>
        <h3 className="text-sm font-medium mb-3" style={{ color: 'var(--color-text-primary)' }}>
          Connected MCP Servers
        </h3>
        {stats.mcp_servers && stats.mcp_servers.length > 0 ? (
          <div className="flex flex-col gap-1">
            {stats.mcp_servers.map(name => (
              <div
                key={name}
                className="flex items-center gap-2.5 py-2 px-3 rounded text-sm"
                style={{ backgroundColor: 'var(--color-bg-card)' }}
              >
                <Server className="h-4 w-4 flex-shrink-0" style={{ color: 'var(--color-accent-primary)' }} />
                <span style={{ color: 'var(--color-text-primary)' }}>{name}</span>
              </div>
            ))}
          </div>
        ) : (
          <div className="py-4 text-center text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
            No MCP servers connected
          </div>
        )}
      </div>

      {/* Skills list */}
      <div>
        <h3 className="text-sm font-medium mb-3" style={{ color: 'var(--color-text-primary)' }}>
          Available Skills
        </h3>
        {stats.skills && stats.skills.length > 0 ? (
          <div className="flex flex-col gap-1">
            {stats.skills.map(skill => (
              <div
                key={skill.name}
                className="flex items-start gap-2.5 py-2 px-3 rounded text-sm"
                style={{ backgroundColor: 'var(--color-bg-card)' }}
              >
                <BookOpen className="h-4 w-4 flex-shrink-0 mt-0.5" style={{ color: 'var(--color-accent-primary)' }} />
                <div className="min-w-0">
                  <span style={{ color: 'var(--color-text-primary)' }}>{skill.name}</span>
                  {skill.description && (
                    <p className="text-xs mt-0.5 line-clamp-2" style={{ color: 'var(--color-text-tertiary)' }}>
                      {skill.description}
                    </p>
                  )}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="py-4 text-center text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
            No skills installed
          </div>
        )}
      </div>

      {/* Sync button */}
      <div
        className="flex flex-col gap-3 pt-3 border-t"
        style={{ borderColor: 'var(--color-border-muted)' }}
      >
        <button
          onClick={onRefresh}
          disabled={refreshing}
          className="flex items-center justify-center gap-2 w-full px-4 py-2.5 text-sm rounded-md transition-colors disabled:opacity-50"
          style={{
            color: 'var(--color-text-primary)',
            border: '1px solid var(--color-border-muted)',
            backgroundColor: 'var(--color-bg-card)',
          }}
        >
          {refreshing ? <Loader size={16} className="text-current" /> : <RefreshCw className="h-4 w-4" />}
          Sync Tools & Skills
        </button>

        {refreshResult && (
          <div
            className="text-xs p-3 rounded"
            style={{
              backgroundColor: 'var(--color-bg-card)',
              color: refreshResult.status === 'error' ? 'var(--color-loss)' : 'var(--color-text-secondary)',
            }}
          >
            {refreshResult.status === 'error' ? (
              refreshResult.message
            ) : (
              <div className="flex flex-col gap-1">
                <span>Tools refreshed: {refreshResult.refreshed_tools ? 'Yes' : 'No'}</span>
                <span>Skills uploaded: {refreshResult.skills_uploaded ? 'Yes' : 'No'}</span>
                {refreshResult.servers && refreshResult.servers.length > 0 && (
                  <span>Servers: {refreshResult.servers.length} connected</span>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}


// ---------------------------------------------------------------------------
// Secrets Tab
// ---------------------------------------------------------------------------

const MAX_SECRETS = 20;

interface VaultSecret {
  workspace_vault_secret_id: string;
  name: string;
  description: string;
  masked_value: string;
  created_at: string;
  updated_at: string;
}

interface SecretsTabProps {
  workspaceId: string;
  /** When set (e.g. via an MCP "Set up NAME" deep-link), opens the add form prefilled. */
  prefillSecretName?: string | null;
  onPrefillConsumed?: () => void;
}

function SecretsTab({ workspaceId, prefillSecretName, onPrefillConsumed }: SecretsTabProps) {
  const { t } = useTranslation();
  const [secrets, setSecrets] = useState<VaultSecret[]>([]);
  const [blueprints, setBlueprints] = useState<VaultBlueprint[]>([]);
  const [remainingSlots, setRemainingSlots] = useState<number>(MAX_SECRETS);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Generation counter: guards against a stale in-flight load clobbering state
  // when workspaceId changes rapidly (A -> B -> A). Increment on entry, check on
  // every resolution path before writing state.
  const loadGenRef = useRef(0);

  async function load() {
    const myGen = ++loadGenRef.current;
    setLoading(true);
    setError(null);
    try {
      // Fetch both in parallel. Blueprints is secondary — a failure there
      // must not block the primary secrets list (graceful degradation).
      const [secretsData, blueprintsData] = await Promise.all([
        getVaultSecrets(workspaceId),
        getVaultBlueprints(workspaceId).catch(() => null),
      ]);
      if (loadGenRef.current !== myGen) return; // superseded, drop this result
      setSecrets(secretsData);
      if (blueprintsData) {
        setBlueprints(blueprintsData.blueprints);
        setRemainingSlots(blueprintsData.remaining_slots);
      } else {
        setBlueprints([]); // hide Recommended section on blueprint fetch failure
        setRemainingSlots(MAX_SECRETS - secretsData.length);
      }
    } catch (err: any) {
      if (loadGenRef.current !== myGen) return;
      setError(err?.response?.data?.detail || err.message || t('vault.loadFailed'));
    } finally {
      if (loadGenRef.current === myGen) {
        setLoading(false);
      }
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId]);

  return (
    <SecretsManager
      // Remount on workspace switch: a half-filled add form from the previous
      // workspace must not carry over and silently target the new one.
      key={workspaceId}
      title={t('vault.workspace.title')}
      secrets={secrets.map((s) => ({
        id: s.workspace_vault_secret_id,
        name: s.name,
        description: s.description,
        masked_value: s.masked_value,
      }))}
      maxSecrets={secrets.length + remainingSlots}
      loading={loading}
      loadError={error}
      emptyText={t('vault.workspace.empty')}
      blueprints={blueprints}
      prefillSecretName={prefillSecretName}
      onPrefillConsumed={onPrefillConsumed}
      onCreate={async (body) => { await createVaultSecret(workspaceId, body); await load(); }}
      onUpdate={async (name, body) => { await updateVaultSecret(workspaceId, name, body); await load(); }}
      onDelete={async (name) => { await deleteVaultSecret(workspaceId, name); await load(); }}
      onReveal={(name) => revealVaultSecret(workspaceId, name)}
      footer={<VaultInfoFooter />}
    />
  );
}

/** Usage + security explainer under the workspace vault list. */
function VaultInfoFooter() {
  const { t } = useTranslation();
  const code = (text: string) => (
    <code className="font-mono" style={{ color: 'var(--color-text-secondary)' }}>{text}</code>
  );
  return (
    <div
      className="flex flex-col gap-2.5 text-xs p-3 rounded-lg mt-1"
      style={{ backgroundColor: 'var(--color-bg-card)', color: 'var(--color-text-tertiary)' }}
    >
      <div>
        <span className="font-medium" style={{ color: 'var(--color-text-secondary)' }}>{t('vault.workspace.usageTitle')}</span>
        <div className="mt-1">
          {t('vault.workspace.usageAccess')} {code('from vault import get; key = get("SECRET_NAME")')}
        </div>
      </div>
      <div
        className="pt-2 flex flex-col gap-1.5"
        style={{ borderTop: '1px solid var(--color-border-muted)' }}
      >
        <span className="font-medium" style={{ color: 'var(--color-text-secondary)' }}>{t('vault.workspace.securityTitle')}</span>
        <ul className="flex flex-col gap-1 pl-3" style={{ listStyleType: 'disc' }}>
          <li>{t('vault.workspace.security1')}</li>
          <li>{t('vault.workspace.security2a')} {code('vault.get()')} {t('vault.workspace.security2b')}</li>
          <li>{t('vault.workspace.security3')}</li>
          <li>{t('vault.workspace.security4a')} {code('vault')} {t('vault.workspace.security4b')}</li>
        </ul>
      </div>
    </div>
  );
}

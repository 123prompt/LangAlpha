import React, { Suspense } from 'react';
import { useTranslation } from 'react-i18next';
import type { ProvenanceRecord } from '@/types/chat';
import type { OpenFileHandler } from '../../utils/fileLocation';
import type { MarketWatchState } from '../../session/marketWatchEvents';
import { SandboxSettingsContent } from '../SandboxSettingsPanel';
import type { SubagentInfo, ToolCallProcessRecord } from '../ToolCallDetailView';
import type { ApiAdapter, ChartTabSpec, ContextPayload, PanelTarget } from './types';
import type { FileTab, FileTabsApi } from './useFileTabs';
import { ChartTab } from './ChartTab';
import { StoreTab } from './StoreTab';
import { FileViewer, type FileViewerProps } from './FileViewer';
import { EmptyTab } from './EmptyTab';

// Tool results draw market-data cards, and those carry the chart stack, so
// like the chart tab they load when looked at rather than with the panel.
const DetailPanel = React.lazy(() => import('../DetailPanel'));
const SourcesPanel = React.lazy(() => import('../SourcesPanel'));

interface ActiveTabBodyProps {
  activeTab: FileTab;
  tabs: FileTabsApi;
  workspaceId: string;
  apiAdapter: ApiAdapter | null;
  target: PanelTarget | null;
  onTargetMemoryHandled?: (seq?: number) => void;
  onTargetMemoHandled?: (seq?: number) => void;
  marketWatch: MarketWatchState | null;
  onOpenFile: OpenFileHandler | null;
  onOpenSubagentTask: ((info: SubagentInfo) => void) | null;
  getToolCallProcess: ((toolCallId: string) => ToolCallProcessRecord | undefined) | null;
  getSourcesRecords: ((messageId: string) => Record<string, ProvenanceRecord> | undefined) | null;
  allSourcesRecords: Record<string, ProvenanceRecord> | undefined;
  onAddContext: ((ctx: ContextPayload) => void) | null;
  onOpenInMarketView: ((spec: ChartTabSpec) => void) | null;
  /** What the file viewer needs beyond the tab's own path: the bytes, the edit and focus state, and the handlers. */
  file: Omit<FileViewerProps, 'path' | 'workspaceId' | 'servePrefix' | 'onPageCount'>;
  onPageCount: (path: string, pages: number) => void;
  /** The empty tab's offers, each null where the panel has none to make. */
  canUpload: boolean;
  treeOpen: boolean;
  onShowTree: (() => void) | null;
  onOpenChart: (() => void) | null;
}

/** What fills the viewer slot for the active tab. Preview panes are rendered beside this, always mounted. */
export function ActiveTabBody({
  activeTab, tabs, workspaceId, apiAdapter, target, onTargetMemoryHandled, onTargetMemoHandled, marketWatch,
  onOpenFile, onOpenSubagentTask, getToolCallProcess, getSourcesRecords, allSourcesRecords, onAddContext,
  onOpenInMarketView, file, onPageCount, canUpload, treeOpen, onShowTree, onOpenChart,
}: ActiveTabBodyProps): React.ReactNode {
  const { t } = useTranslation();
  switch (activeTab.kind) {
    case 'preview':
      return null;
    case 'chart':
      return (
        <ChartTab
          tab={activeTab}
          tabs={tabs}
          workspaceId={workspaceId}
          onAddContext={onAddContext}
          onOpenInMarketView={onOpenInMarketView}
        />
      );
    case 'settings':
      return (
        <div className="file-panel-settings">
          <SandboxSettingsContent workspaceId={workspaceId} />
        </div>
      );
    case 'tool': {
      // The tab holds only the id, so a record cleared from the chat (a
      // subagent card dismissed, say) leaves it nothing to draw.
      const toolCallProcess = getToolCallProcess?.(activeTab.toolCallId) ?? null;
      if (!toolCallProcess) {
        return (
          <p className="px-6 py-10 text-center text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            {t('toolArtifact.toolCallGone')}
          </p>
        );
      }
      return (
        <Suspense fallback={null}>
          <DetailPanel
            key={activeTab.toolCallId}
            toolCallProcess={toolCallProcess}
            onOpenFile={onOpenFile ?? undefined}
            onOpenSubagentTask={onOpenSubagentTask ?? undefined}
          />
        </Suspense>
      );
    }
    case 'plan':
      return (
        <Suspense fallback={null}>
          <DetailPanel key={activeTab.planId} toolCallProcess={null} planData={activeTab.plan} />
        </Suspense>
      );
    case 'memory':
    case 'memo':
    case 'status':
      return (
        <StoreTab
          kind={activeTab.kind}
          workspaceId={workspaceId}
          target={target}
          onTargetMemoryHandled={onTargetMemoryHandled}
          onTargetMemoHandled={onTargetMemoHandled}
          onOpenFile={onOpenFile}
          marketWatch={marketWatch}
        />
      );
    case 'sources':
      return (
        <Suspense fallback={null}>
          <SourcesPanel
            provenanceRecords={getSourcesRecords?.(activeTab.messageId)}
            allRecords={allSourcesRecords}
            onOpenFile={onOpenFile ?? undefined}
          />
        </Suspense>
      );
    case 'file': {
      const path = activeTab.path;
      return (
        <FileViewer
          {...file}
          path={path}
          workspaceId={workspaceId}
          onPageCount={(pages) => onPageCount(path, pages)}
          servePrefix={apiAdapter?.servePrefix}
        />
      );
    }
    case 'empty':
      return <EmptyTab canUpload={canUpload} treeOpen={treeOpen} onShowTree={onShowTree} onOpenChart={onOpenChart} />;
    default:
      return activeTab satisfies never;
  }
}

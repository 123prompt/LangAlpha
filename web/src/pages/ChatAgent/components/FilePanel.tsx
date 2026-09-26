import React, { useCallback, useEffect, useId, useMemo, useRef, useState, Suspense } from 'react';
import { TextSelect, Upload } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import {
  memoMimeForName,
  useAddToMemo,
  useWorkspaceMemoIndex,
  useMemoStaleCheck,
  MemoStaleBanner,
  MemoDiffModal,
} from './FilePanelMemo';
import { useWorkspace } from '@/hooks/useWorkspace';
import { useIsMobile } from '@/hooks/useIsMobile';
import { useNarrowContainer } from '@/hooks/useNarrowContainer';
import {
  readWorkspaceFile, readWorkspaceFileFull, writeWorkspaceFile, downloadWorkspaceFileAsArrayBuffer,
  triggerFileDownload, resolveWorkspaceFile,
} from '../utils/api';
import { linkCandidates } from '../utils/fileRefResolver';
import type { WriteEvent } from '../utils/fileRefResolver';
import { classifyAgentPath, parseAgentPath } from '../utils/agentPaths';
import { useStableHandler } from '@/hooks/useStableHandler';
import { parseFragment, type FileLocation, type OpenFileHandler } from '../utils/fileLocation';
import FileHeaderActions from './FileHeaderActions';
import { useDownloadState, workspaceDownloadKey } from '../utils/downloadNotice';
import './FilePanel.css';

const ExportPreviewModal = React.lazy(() => import('./ExportPreviewModal'));

import type { ApiAdapter, ChartTabSpec, ContextPayload, PanelTarget } from './filePanel/types';
import { EDITABLE_EXTENSIONS, getFileExtension, viewerFor } from './filePanel/fileMeta';
import { useFileUpload } from './filePanel/useFileUpload';
import { useFileEdit } from './filePanel/useFileEdit';
import { useSelectionContext } from './filePanel/useSelectionContext';
import { useFileSelection } from './filePanel/useFileSelection';
import { useFileBackup } from './filePanel/useFileBackup';
import { useFileFocus } from './filePanel/useFileFocus';
import { FocusChip } from './filePanel/FocusChip';
import { useFileTabs, isListingTab, lastChartSymbol, type FileTab } from './filePanel/useFileTabs';
import { useTreeFilter } from './filePanel/useTreeFilter';
import { useTreeInteraction } from './filePanel/useTreeInteraction';
import { useFileRefOpen } from './filePanel/useFileRefOpen';
import { PanelNotices } from './filePanel/PanelNotices';
import { FileContextMenu, type FileMenuAction } from './filePanel/FileContextMenu';
import { useFileDownloads } from './filePanel/useFileDownloads';
import { useFileBodyCache, useFileBody } from './filePanel/useFileBody';
import { useChangedFiles } from './filePanel/useChangedFiles';
import { countLines } from '../utils/fileLocation';
import { TabStrip } from './filePanel/TabStrip';
import { AnimatePresence } from 'framer-motion';
import { TreeColumn } from './filePanel/TreeColumn';
import { FileCrumbs } from './filePanel/FileCrumbs';
import { PreviewCrumbs } from './filePanel/PreviewCrumbs';
import { PreviewPanes } from './filePanel/PreviewPanes';
import { usePreviews } from './filePanel/usePreviews';
import { usePanelTarget } from './filePanel/usePanelTarget';
import { ActiveTabBody } from './filePanel/ActiveTabBody';
import { useWatchTab } from './filePanel/useWatchTab';
import type { MarketWatchState } from '../session/marketWatchEvents';
import { RouteLeaveGuardContext, type RouteLeaveGuard } from '../contexts/RouteLeaveGuardContext';
import type { SubagentInfo, ToolCallProcessRecord } from './ToolCallDetailView';
import { countDedupedSources, type ProvenanceRecord } from '@/types/chat';

/** Below this the tree cannot be a column without starving the viewer. */
const TREE_OVERLAY_WIDTH = 720;

interface FilePanelProps {
  workspaceId: string;
  /** Scopes the tab strip; absent for a chat that has not sent its first message, or a share. */
  threadId?: string | null;
  onClose: () => void;
  /** What the chat asked the panel to show. A `file` target's `dir` stays on
   *  as the tree's scope until `onTargetHandled` clears it; a memory or memo
   *  target stays until its tab has selected the entry. Each handled callback
   *  names the ask's `seq`, so a clear cannot drop an ask that landed since. */
  target?: PanelTarget | null;
  onTargetHandled?: (seq?: number) => void;
  onTargetMemoryHandled?: (seq?: number) => void;
  onTargetMemoHandled?: (seq?: number) => void;
  /** The live market watch, which the Status tab follows. */
  marketWatch?: MarketWatchState | null;
  /** Leaves the panel for the full MarketView page on this symbol. */
  onOpenInMarketView?: ((spec: ChartTabSpec) => void) | null;
  /** Leaves for a subagent's own transcript, from a tool tab showing its task. */
  onOpenSubagentTask?: ((info: SubagentInfo) => void) | null;
  /** A tool call's live record, for a tool tab; read on every render so a running call's result shows when it lands. */
  getToolCallProcess?: ((toolCallId: string) => ToolCallProcessRecord | undefined) | null;
  /** A turn's live provenance, for a sources tab; read on every render so records streaming in show. */
  getSourcesRecords?: ((messageId: string) => Record<string, ProvenanceRecord> | undefined) | null;
  /** Every turn's provenance merged, the sources tab's "All sources" scope; read only while that tab is showing. */
  getAllSourcesRecords?: (() => Record<string, ProvenanceRecord> | undefined) | null;
  /** Opens a reference to another workspace (a `__wsref__` link inside a viewed file). */
  onOpenFile?: OpenFileHandler | null;
  /** This thread's Write/Edit paths, newest first, for resolving a reference by name. */
  getRecentWritePaths?: (() => string[]) | null;
  /** Every Write/Edit in the thread, newest first; what marks an open tab changed. */
  getWriteLog?: (() => WriteEvent[]) | null;
  files?: string[];
  filesLoading?: boolean;
  filesError?: string | null;
  onRefreshFiles?: () => void;
  readOnly?: boolean;
  /** Whether this viewer may save a file's bytes. A copy-link share grants
   *  `allow_files` without `allow_download`, and the download endpoint refuses
   *  what `allow_files` alone opened, so an offered save fails after the click. */
  canDownload?: boolean;
  /** Lock to one file: no tree, and closing the last tab closes the panel. */
  singleFileMode?: boolean;
  /** False keeps the strip in memory only: a panel browsing beside a gallery
   *  must not write over the strip the workspace's conversations seed from. */
  persistTabs?: boolean;
  /** False for a panel mounted off screen (a recently visited thread the chat
   *  keeps warm): it holds its strip but does not save it, since the saved
   *  strip is the one the reader is looking at. */
  isActive?: boolean;
  apiAdapter?: ApiAdapter | null;
  onAddContext?: ((ctx: ContextPayload) => void) | null;
  showSystemFiles?: boolean;
  onToggleSystemFiles?: (() => void) | null;
  /** Whether any open tab holds an unsaved edit. Whoever can unmount the
   *  panel reads this to ask before doing so. */
  onDirtyChange?: ((dirty: boolean) => void) | null;
  /** What kind of tab is in front, as it changes; null once the panel is gone.
   *  The host that sizes the panel reads this, since a chart has a floor of
   *  its own. */
  onActiveTabKindChange?: ((kind: FileTab['kind'] | null) => void) | null;
  /** Offer the open file's share dialog in its header: the owner's own panel only. */
  canShare?: boolean;
}

function FilePanel({
  workspaceId,
  threadId = null,
  onClose,
  target = null,
  onTargetHandled,
  onTargetMemoryHandled,
  onTargetMemoHandled,
  marketWatch = null,
  onOpenInMarketView = null,
  onOpenSubagentTask = null,
  getToolCallProcess = null,
  getSourcesRecords = null,
  getAllSourcesRecords = null,
  onOpenFile = null,
  getRecentWritePaths = null,
  getWriteLog = null,
  files = [],
  filesLoading = false,
  filesError = null,
  onRefreshFiles,
  readOnly = false,
  canDownload = true,
  singleFileMode = false,
  persistTabs = true,
  isActive = true,
  apiAdapter = null,
  onAddContext = null,
  showSystemFiles = false,
  onToggleSystemFiles = null,
  onDirtyChange = null,
  onActiveTabKindChange = null,
  canShare = false,
}: FilePanelProps): React.ReactElement {
  const { t } = useTranslation();
  const isMobile = useIsMobile();
  const panelRef = useRef<HTMLDivElement>(null);
  const narrow = useNarrowContainer(panelRef, TREE_OVERLAY_WIDTH);

  // A share reads through its own endpoints, which take no workspace id.
  // Memoised as one object: every hook below closes over these, and rebuilding
  // them per render would make each of those callbacks unstable in turn.
  const { readFileFn, readFileFullFn, downloadFileAsArrayBufferFn, triggerDownloadFn, writeFileFn, resolveFileFn } = useMemo(() => {
    const adapter: ApiAdapter = apiAdapter ?? {};
    const { readFile, readFileFull, downloadFileAsArrayBuffer, triggerDownload, writeFile, resolveFile } = adapter;
    return {
      readFileFn: readFile ? (_: string, p: string) => readFile(p) : readWorkspaceFile,
      readFileFullFn: readFileFull ? (_: string, p: string) => readFileFull(p) : readWorkspaceFileFull,
      downloadFileAsArrayBufferFn: downloadFileAsArrayBuffer
        ? (_: string, p: string) => downloadFileAsArrayBuffer(p)
        : downloadWorkspaceFileAsArrayBuffer,
      triggerDownloadFn: triggerDownload ? (_: string, p: string) => triggerDownload(p) : triggerFileDownload,
      writeFileFn: writeFile ? (_: string, p: string, c: string) => writeFile(p, c) : writeWorkspaceFile,
      resolveFileFn: apiAdapter
        ? resolveFile ?? null
        : (candidates: string[], recentWrites: string[]) => resolveWorkspaceFile(workspaceId, candidates, recentWrites),
    };
  }, [apiAdapter, workspaceId]);

  const { data: wsData } = useWorkspace(workspaceId);
  const isFlashWorkspace = wsData?.status === 'flash';
  // The reader's memory and memo stores open here as tabs. A share reads
  // through an adapter with no workspace id: it browses someone else's files
  // and has no stores to show.
  const stores = !!workspaceId;
  // A file the tree never got is not a file that is gone, so this flag also
  // decides what a missed reference is told.
  const filesRestoreIncomplete = wsData?.files_restore_incomplete === true;

  // A share has no workspace id of its own, so its bodies are scoped to this
  // mount: two shares open at once must not read each other's bytes.
  const mountId = useId();
  const scope = workspaceId || `adapter:${mountId}`;
  const readers = useMemo(
    () => ({ readFile: readFileFn, readFileFull: readFileFullFn, downloadFileAsArrayBuffer: downloadFileAsArrayBufferFn }),
    [readFileFn, readFileFullFn, downloadFileAsArrayBufferFn],
  );
  const cache = useFileBodyCache({ scope, workspaceId, readers });

  // A peek at one file borrows the PTC workspace's id for its reads; its strip
  // must not become that workspace's seed, so it is kept in memory only, and
  // the last chart symbol is neither read nor left behind. The strip is still
  // this workspace's: pointing the panel at another one starts that
  // workspace's strip rather than leaving these tabs under the new id.
  const persistStrip = !singleFileMode && persistTabs;
  const tabs = useFileTabs(workspaceId, threadId, { persist: persistStrip, active: isActive });
  const activeTab = tabs.activeTab;
  // The tree lists the workspace's files, so it sits beside a file or the
  // empty tab and nothing else: a chart, a tool result or an app is its own
  // surface, and the listing (and its notices) has no business there.
  const listingTab = isListingTab(activeTab);
  const selectedFile = activeTab.kind === 'file' ? activeTab.path : null;

  const previews = usePreviews(workspaceId);

  const { body, loading: fileLoading, error: readError, readAt, refetch } = useFileBody({
    cache, path: selectedFile, workspaceStatus: wsData?.status,
  });
  const fileContent = body?.content ?? null;
  const fileMime = body?.mime ?? null;

  const changed = useChangedFiles(getWriteLog);
  useEffect(() => {
    if (selectedFile && readAt) changed.markRead(selectedFile);
  }, [selectedFile, readAt]); // eslint-disable-line react-hooks/exhaustive-deps
  // The read marks die with this mount, so the bytes they describe must too:
  // a panel reopened inside the body's fresh window would otherwise show
  // bytes written over while it was away and stamp that write as read. With
  // no observer left, marking stale issues no request; the active tab
  // re-reads on the way back in.
  const dropBodies = useStableHandler(() => cache.invalidate());
  useEffect(() => () => dropBodies(), [dropBodies]);

  const downloads = useFileDownloads({ workspaceId, triggerDownloadFn, workspaceStatus: wsData?.status });
  const fileError = downloads.errorFor(selectedFile) ?? readError;

  const [exportModalOpen, setExportModalOpen] = useState(false);
  const [pageCounts, setPageCounts] = useState<Record<string, number>>({});

  const { uploadProgress, uploadError, setUploadError, fileInputRef, isDragOver, handleFileInputChange, handleDragEnter, handleDragLeave, handleDragOver, handleDrop } =
    useFileUpload({ workspaceId, onRefreshFiles });

  const setFileContent = useCallback((next: React.SetStateAction<string | null>) => {
    if (!selectedFile) return;
    const value = typeof next === 'function' ? next(fileContent) : next;
    cache.patchBody(selectedFile, { content: value, truncated: false });
  }, [cache, selectedFile, fileContent]);

  const edit = useFileEdit({
    tabId: activeTab.id, workspaceId, selectedFile, fileContent, setFileContent, readFileFullFn, writeFileFn,
  });

  // Every draft, parked or on screen, lives in this mount and dies with it. A
  // wrapper that owns the close button therefore has to ask before it unmounts
  // the panel, and can only know to when the panel says so. Reporting clean on
  // the way out keeps it from asking about a panel that is already gone.
  const reportDirty = useStableHandler((dirty: boolean) => onDirtyChange?.(dirty));
  useEffect(() => { reportDirty(edit.hasAnyUnsavedChanges); }, [edit.hasAnyUnsavedChanges, reportDirty]);
  useEffect(() => () => reportDirty(false), [reportDirty]);

  // Reported on mount too, for a strip restored with a chart in front.
  const reportActiveKind = useStableHandler((kind: FileTab['kind'] | null) => onActiveTabKindChange?.(kind));
  useEffect(() => { reportActiveKind(activeTab.kind); }, [activeTab.kind, reportActiveKind]);
  useEffect(() => () => reportActiveKind(null), [reportActiveKind]);

  const { selectionTooltip, contentWrapperRef, contextMenu, setContextMenu, handleContentMouseUp, handleEditorTextSelect, handleAddSelectionContext } =
    useSelectionContext({ selectedFile, fileContent, onAddContext });

  const viewer = selectedFile ? viewerFor(selectedFile, fileMime, edit.isEditing) : 'other';
  const focus = useFileFocus({
    selectedFile,
    viewer,
    ready: !fileLoading && !fileError,
    editing: edit.isEditing,
    content: fileContent,
    truncated: !!body?.truncated,
    pageCount: selectedFile ? pageCounts[selectedFile] ?? null : null,
    containerRef: contentWrapperRef,
  });

  // Replay the tab's own location whenever it comes back to the front.
  const tabLocationSeq = activeTab.kind === 'file' ? activeTab.locationSeq : 0;
  useEffect(() => {
    if (activeTab.kind === 'file' && activeTab.location) focus.focusAt(activeTab.path, activeTab.location);
  }, [activeTab.id, tabLocationSeq]); // eslint-disable-line react-hooks/exhaustive-deps

  // The folder a chat link pointed the tree at; it filters until the back
  // button clears it. The panel's own state rather than a reading of the
  // target: the target clears once handled, like every other kind, so a
  // remount does not replay the ask and pop the tree open again.
  const [scopeDir, setScopeDir] = useState<string | null>(() => (target?.kind === 'file' ? target.dir ?? null : null));
  const filter = useTreeFilter({ workspaceId, files, scopeDir, rootRef: panelRef });
  // A deleted file's tab goes with it, draft and cached bytes included, or the
  // strip keeps showing a file the tree no longer lists and a save would write
  // it back. No confirm: the reader just confirmed the delete, and the write
  // log never records a panel-side delete, so the close path's change marker
  // would not drop the body on its own.
  const forgetDeleted = useCallback((paths: string[]) => {
    const gone = new Set(paths);
    for (const tab of tabs.tabs) {
      if (tab.kind !== 'file' || !gone.has(tab.path)) continue;
      edit.forgetTab(tab.id);
      changed.forget(tab.path);
      cache.invalidate(tab.path);
      tabs.closeTab(tab.id);
    }
  }, [tabs, edit, changed, cache]);
  const selection = useFileSelection({ workspaceId, filteredSortedFiles: filter.filteredSortedFiles, targetDirectory: scopeDir, onRefreshFiles, onDeleted: forgetDeleted });
  const backup = useFileBackup({ workspaceId, files, readOnly });

  // `openFileAt` is defined below, so the tree reaches it through a handler
  // that stays the same object across renders.
  const openFromTree = useStableHandler((path: string) => { void openFileAt(path); });
  const tree = useTreeInteraction({
    workspaceId, rootRef: panelRef, selection, narrow, activePath: selectedFile, openFile: openFromTree,
  });
  const { open: treeOpen, setOpen: setTreeOpen } = tree;
  const treeShown = treeOpen && !singleFileMode && listingTab;

  /** A breadcrumb segment points the tree at that directory. */
  const revealInTree = useCallback((dir: string) => {
    setTreeOpen(true);
    filter.revealDir(dir);
  }, [setTreeOpen, filter]);

  /** Nothing the panel is showing over the viewer survives a file landing in it. */
  const onBeforeOpen = useCallback(() => {
    downloads.clearError();
  }, [downloads]);

  /** A reference nothing could settle: the tree, filtered to the name it used. */
  const landOnSearch = useCallback((ref: string, name: string, matches: string[]) => {
    filter.showMatches(ref, name, matches);
    tabs.showListing();
    setTreeOpen(true);
    // A folder scope would hide candidates outside it, so it goes.
    setScopeDir(null);
  }, [filter, tabs, setTreeOpen]);

  const { openFileAt, openFileRef, retryOpen, cancelPending } = useFileRefOpen({
    tabs,
    cache,
    hasChanged: changed.hasChanged,
    files,
    workspaceStatus: wsData?.status,
    resolveFileFn,
    getRecentWritePaths,
    onBeforeOpen,
    clearSearch: filter.clearSearch,
    onLandOnSearch: landOnSearch,
    refetch,
  });

  usePanelTarget({
    target, tabs, previews, openFileRef, cancelPending,
    clearSearch: filter.clearSearch, setTreeOpen, setScopeDir, onTargetHandled,
  });

  // Opening a singleton tab drops a reference still resolving, like any other
  // open. Each store is one tab, so "view in memo" lands on the Memo tab in
  // place and leaves every draft parked where it is.
  const openSettings = useCallback(() => { cancelPending(); tabs.openSettings(); }, [tabs, cancelPending]);
  const openMemory = useCallback(() => { cancelPending(); tabs.openMemory(); }, [tabs, cancelPending]);
  const openMemo = useCallback(() => { cancelPending(); tabs.openMemo(); }, [tabs, cancelPending]);
  const viewInMemo = stores ? openMemo : null;

  const handleAddToMemo = useAddToMemo({ workspaceId, downloadFileAsArrayBufferFn, readFileFullFn, onSwitchToMemoTab: viewInMemo });
  const memoedMap = useWorkspaceMemoIndex(workspaceId);
  const memoEntry = selectedFile ? memoedMap.get(selectedFile) ?? null : null;
  const { status: memoStaleStatus, sandboxText: memoStaleSandboxText, refresh: refreshMemoStale } = useMemoStaleCheck({
    workspaceId, selectedFile, fileMime, memoSha256: memoEntry?.sha256 ?? null, readFileFullFn,
  });
  const [memoSyncing, setMemoSyncing] = useState(false);
  const [memoDiffOpen, setMemoDiffOpen] = useState(false);

  useWatchTab(tabs, marketWatch);

  const sourceCount = useCallback(
    (messageId: string) => countDedupedSources(getSourcesRecords?.(messageId)),
    [getSourcesRecords],
  );

  // Merged only while a sources tab is showing: the accessor is remade per
  // transcript change, so a thread streaming with a file in front never pays
  // for a merge nothing reads.
  const allSourcesRecords = useMemo(
    () => (activeTab.kind === 'sources' ? getAllSourcesRecords?.() : undefined),
    [activeTab.kind, getAllSourcesRecords],
  );

  const retry = useCallback(() => {
    downloads.clearError();
    retryOpen(selectedFile);
  }, [downloads, retryOpen, selectedFile]);

  // --- file actions ---

  const selectedDownloadState = useDownloadState(
    selectedFile ? workspaceDownloadKey(workspaceId, selectedFile) : null,
  );
  const contextMenuDownloadState = useDownloadState(
    contextMenu ? workspaceDownloadKey(workspaceId, contextMenu.filePath) : null,
  );
  const handleDownloadSelected = canDownload && selectedFile ? () => downloads.download(selectedFile) : undefined;
  const handleDownloadInFallback = canDownload && selectedFile ? () => downloads.downloadQuietly(selectedFile) : undefined;

  const handleContextMenuAction = useCallback((action: FileMenuAction, filePath: string) => {
    setContextMenu(null);
    if (action === 'add-context' && onAddContext) {
      tabs.openFile(filePath, { pin: true });
      onAddContext({ path: filePath });
    } else if (action === 'add-to-memo') {
      handleAddToMemo(filePath);
    } else if (action === 'open') {
      void openFileAt(filePath);
    } else if (action === 'open-new-tab') {
      void openFileAt(filePath, { pin: true });
    } else if (action === 'download') {
      downloads.download(filePath);
    } else if (action === 'download-many') {
      void downloads.downloadMany([...selection.selectedPaths]);
    }
  }, [onAddContext, handleAddToMemo, openFileAt, downloads, selection.selectedPaths, setContextMenu, tabs]);

  /**
   * Coming back to a tab whose file the agent has rewritten re-reads it. The
   * amber dot is the notice; arriving at the tab is the moment the reader
   * wants the new bytes, and re-reading every open tab the instant a write
   * lands would fight whoever is reading one of them.
   */
  const activateTab = useCallback((id: string) => {
    const tab = tabs.tabs.find((x) => x.id === id);
    if (tab?.kind === 'file' && changed.hasChanged(tab.path)) cache.invalidate(tab.path);
    cancelPending();
    tabs.activate(id);
  }, [tabs, changed, cache, cancelPending]);

  const newTab = useCallback(() => {
    cancelPending();
    tabs.newTab();
  }, [tabs, cancelPending]);

  const startEdit = useCallback(() => {
    tabs.pinTab(activeTab.id);
    void edit.handleStartEdit();
  }, [tabs, activeTab.id, edit]);

  // Citing a range is working with the file, so its tab stops being the loaned
  // one, the same rule the selection tooltip and the tree's menu follow.
  const addViewerContext = useCallback((ctx: ContextPayload) => {
    tabs.pinTab(activeTab.id);
    onAddContext?.(ctx);
  }, [tabs, activeTab.id, onAddContext]);

  // Every way out of this mount asks the question closing a dirty tab asks: a
  // route change fires no beforeunload, so the drafts would go with it. The
  // same guard is handed down to the tool and plan tabs, whose result views
  // carry links off the route.
  const guardLeave = useCallback<RouteLeaveGuard>((go) => {
    if (edit.hasAnyUnsavedChanges && !window.confirm(t('filePanel.discardUnsaved'))) return;
    go();
  }, [edit.hasAnyUnsavedChanges, t]);

  const leaveForMarketView = useCallback((spec: ChartTabSpec) => {
    guardLeave(() => onOpenInMarketView?.(spec));
  }, [guardLeave, onOpenInMarketView]);

  // Always offered: on mobile the panel covers the chat, and a close that went
  // away with the draft left no way back.
  const closePanel = useCallback(() => { guardLeave(onClose); }, [guardLeave, onClose]);

  const closeTab = useCallback((id: string) => {
    if (edit.tabHasUnsavedChanges(id) && !window.confirm(t('filePanel.discardUnsaved'))) return;
    edit.forgetTab(id);
    const tab = tabs.tabs.find((x) => x.id === id);
    if (tab?.kind === 'file') {
      // The marker is what forces a re-read on reopen; the cached bytes must
      // not outlive it, or a rewrite lands inside the body's fresh window.
      if (changed.hasChanged(tab.path)) cache.invalidate(tab.path);
      changed.forget(tab.path);
    }
    if (singleFileMode && tabs.tabs.length <= 1) return onClose();
    tabs.closeTab(id);
  }, [edit, tabs, changed, cache, singleFileMode, onClose, t]);

  const handleViewerLink = useStableHandler((path: string, linkWorkspaceId?: string, location?: FileLocation, opts?: { rooted?: boolean; pin?: boolean }) => {
    const rooted = !!opts?.rooted;
    const otherWorkspace = !!linkWorkspaceId && linkWorkspaceId !== workspaceId;
    const kind = classifyAgentPath(path).kind;
    const directory = parseAgentPath(path).directory;
    // A folder inside a viewed file is written relative to it, the same as a
    // file link, but the router takes the path as given, so the join happens here.
    const linkTarget = directory && kind === 'file' && !otherWorkspace
      ? linkCandidates(path, rooted ? null : selectedFile)[0]
      : path;
    if (otherWorkspace || kind !== 'file' || directory) {
      onOpenFile?.(linkTarget, linkWorkspaceId, location);
      return;
    }
    // A rooted reference named where it starts, so the open file's directory is
    // not a reading it invited.
    void openFileRef(path, { fromFile: rooted ? null : selectedFile, location, pin: opts?.pin });
  });

  const handleAnchorLink = useStableHandler((fragment: string) => {
    if (selectedFile) focus.focusAt(selectedFile, parseFragment(fragment));
  });

  const handleSyncMemo = useCallback(async () => {
    if (!selectedFile || memoSyncing) return;
    setMemoSyncing(true);
    try {
      await handleAddToMemo(selectedFile);
      refreshMemoStale();
    } finally {
      setMemoSyncing(false);
    }
  }, [selectedFile, memoSyncing, handleAddToMemo, refreshMemoStale]);

  // A write lands in the sandbox, so the copy the panel holds and the backup
  // verdict beside it are both a version behind until they are re-read.
  const handleSave = useCallback(async () => {
    await edit.handleSave();
    if (selectedFile) cache.invalidate(selectedFile);
    onRefreshFiles?.();
  }, [edit, cache, selectedFile, onRefreshFiles]);

  // Editing needs a text viewer under it: the pdf, excel and html readers are
  // not editors, and an image or a parked tool result is not text. A CSV reads
  // in the grid (`other`) but edits as the text it is; a file already in the
  // editor keeps its verdict rather than reading the editor as `other`.
  const selectedExt = selectedFile ? getFileExtension(selectedFile) : '';
  const canEdit = !!(selectedFile && !readOnly && !fileError
    && EDITABLE_EXTENSIONS.has(selectedExt)
    && (edit.isEditing || viewer === 'code' || viewer === 'markdown' || (viewer === 'other' && selectedExt === 'csv' && fileMime !== 'image')));

  const meta = useMemo(() => {
    if (!selectedFile || !body) return null;
    const pages = pageCounts[selectedFile];
    if (body.mime === 'pdf') return pages ? t('filePanel.metaPages', { count: pages }) : null;
    if (body.content == null) return null;
    const lines = countLines(body.content);
    return body.truncated ? t('filePanel.metaFirstLines', { count: lines }) : t('filePanel.metaLines', { count: lines });
  }, [selectedFile, body, pageCounts, t]);

  /** Opening a running app from the tree: the tab it already has, or a new one. */
  const openPreviewTab = useCallback((port: number) => {
    const entry = previews.byPort.get(port);
    cancelPending();
    tabs.openPreview({ port, title: entry?.title, path: entry?.path, command: entry?.command });
    previews.ensure(port);
  }, [previews, tabs, cancelPending]);

  // Only a tab the tree sits beside takes a drop; every other kind shows
  // something that is not a folder.
  const canDropHere = !readOnly && listingTab;

  const onPageCount = useCallback((path: string, pages: number) => {
    setPageCounts((prev) => (prev[path] === pages ? prev : { ...prev, [path]: pages }));
  }, []);

  return (
    <RouteLeaveGuardContext.Provider value={guardLeave}>
    <div className="file-panel" ref={panelRef} onKeyDown={tree.onEscape}>
      <TabStrip
        tabs={tabs.tabs}
        activeId={tabs.activeId}
        onActivate={activateTab}
        onClose={closeTab}
        onPin={tabs.pinTab}
        sourceCount={sourceCount}
        getToolCallProcess={getToolCallProcess ?? undefined}
        onNewTab={singleFileMode || readOnly ? null : newTab}
        hasChanged={changed.hasChanged}
        treeOpen={treeShown}
        onToggleTree={singleFileMode || !listingTab ? null : () => setTreeOpen((v) => !v)}
        // Locked to one file, the panel has no tree to pin the stores in, so
        // the strip offers them instead.
        onOpenMemory={singleFileMode && stores ? openMemory : null}
        onOpenMemo={singleFileMode ? viewInMemo : null}
        onPanelClose={closePanel}
        backArrow={isMobile}
      />

      {activeTab.kind === 'preview' && (
        <PreviewCrumbs
          entry={previews.byPort.get(activeTab.port) ?? { port: activeTab.port, url: '', loading: true, error: false, reloadToken: 0 }}
          onRefresh={() => previews.refresh(activeTab.port)}
          workspaceId={readOnly ? null : workspaceId}
        />
      )}

      {selectedFile && (
        <FileCrumbs
          path={selectedFile}
          onOpenDir={revealInTree}
          meta={meta}
          unsaved={edit.hasUnsavedChanges}
          chip={focus.chip && (
            <FocusChip state={focus.chip} onJump={focus.jump} onDismiss={() => { focus.dismiss(); tabs.clearLocation(activeTab.id); }} />
          )}
          actions={(
            <FileHeaderActions
              selectedFile={selectedFile}
              isEditing={edit.isEditing}
              workspaceId={workspaceId}
              fileContent={fileContent}
              fileMime={fileMime}
              canEdit={canEdit}
              onStartEdit={startEdit}
              onOpenExportModal={() => setExportModalOpen(true)}
              triggerDownloadFn={triggerDownloadFn}
              canDownload={canDownload}
              readFileFullFn={readFileFullFn}
              servePrefix={apiAdapter?.servePrefix}
              canShare={canShare}
              editorRef={edit.editorRef}
              canUndo={edit.canUndo}
              canRedo={edit.canRedo}
              hasUnsavedChanges={edit.hasUnsavedChanges}
              showDiff={edit.showDiff}
              setShowDiff={edit.setShowDiff}
              isSaving={edit.isSaving}
              saveError={edit.saveError}
              onSave={handleSave}
              onCancelEdit={edit.handleCancelEdit}
            />
          )}
        />
      )}

      <PanelNotices
        uploadProgress={uploadProgress}
        error={uploadError || selection.deleteError}
        onDismissError={() => { setUploadError(null); selection.setDeleteError(null); }}
        // The tree shows this itself; the body takes it over while the tree is
        // folded or absent, so a listing that failed is never a silent blank.
        // A tab that is not a listing carries neither notice.
        filesError={treeShown || !listingTab ? null : filesError}
        filesRestoreIncomplete={treeShown || !listingTab ? false : filesRestoreIncomplete}
        onRefreshFiles={onRefreshFiles}
        busy={selection.deleteLoading || backup.backingUp}
        backupResult={backup.backupResult}
        onDismissBackupResult={() => backup.setBackupResult(null)}
        editing={edit.isEditing}
      />
      {memoEntry && selectedFile && (
        <MemoStaleBanner
          status={memoStaleStatus}
          syncing={memoSyncing}
          onSwitchToMemoTab={viewInMemo}
          onSync={handleSyncMemo}
          onViewDiff={memoStaleSandboxText !== null ? () => setMemoDiffOpen(true) : null}
        />
      )}

        <div className="file-panel-body">
          <div
            className="file-panel-viewer"
            onDragEnter={canDropHere ? handleDragEnter : undefined}
            onDragLeave={canDropHere ? handleDragLeave : undefined}
            onDragOver={canDropHere ? handleDragOver : undefined}
            onDrop={canDropHere ? handleDrop : undefined}
          >
            {canDropHere && isDragOver && (
              <div className="file-panel-drag-overlay">
                <Upload className="h-8 w-8" style={{ color: 'var(--color-accent-primary)' }} />
                <span>{t('filePanel.dropToUpload')}</span>
              </div>
            )}
            {/* font-content only while reading a file: the tree stays on the UI font. */}
            <div className={`file-panel-content${selectedFile ? ' font-content' : ''}`} ref={contentWrapperRef}>
              {selectionTooltip && onAddContext && (
                <button
                  type="button"
                  className="file-panel-selection-tooltip"
                  style={{ left: Math.max(8, selectionTooltip.x - 60), top: Math.max(4, selectionTooltip.y - 32) }}
                  // Acts on mousedown: a click would land after the browser has
                  // already collapsed the selection it is meant to capture.
                  onMouseDown={(e: React.MouseEvent) => {
                    e.preventDefault(); e.stopPropagation();
                    tabs.pinTab(activeTab.id);
                    handleAddSelectionContext();
                  }}
                  onKeyDown={(e: React.KeyboardEvent) => {
                    if (e.key !== 'Enter' && e.key !== ' ') return;
                    e.preventDefault();
                    tabs.pinTab(activeTab.id);
                    handleAddSelectionContext();
                  }}
                >
                  <TextSelect className="h-3.5 w-3.5" style={{ color: 'var(--color-accent-primary)' }} />
                  {selectionTooltip.lineStart != null
                    ? (selectionTooltip.lineEnd !== selectionTooltip.lineStart
                        ? t('context.addLinesToContext', { start: selectionTooltip.lineStart, end: selectionTooltip.lineEnd })
                        : t('context.addLineToContext', { line: selectionTooltip.lineStart }))
                    : t('context.addToContext')}
                </button>
              )}
              <PreviewPanes tabs={tabs.tabs} activeId={activeTab.id} previews={previews} />
              <ActiveTabBody
                activeTab={activeTab}
                tabs={tabs}
                workspaceId={workspaceId}
                apiAdapter={apiAdapter}
                target={target}
                onTargetMemoryHandled={onTargetMemoryHandled}
                onTargetMemoHandled={onTargetMemoHandled}
                marketWatch={marketWatch}
                onOpenFile={onOpenFile}
                onOpenSubagentTask={onOpenSubagentTask}
                getToolCallProcess={getToolCallProcess}
                getSourcesRecords={getSourcesRecords}
                allSourcesRecords={allSourcesRecords}
                onAddContext={onAddContext}
                onOpenInMarketView={onOpenInMarketView ? leaveForMarketView : null}
                file={{
                  body,
                  loading: fileLoading,
                  error: fileError,
                  onRetry: retry,
                  onDownload: handleDownloadSelected,
                  downloadState: selectedDownloadState,
                  onDownloadInFallback: handleDownloadInFallback,
                  focus,
                  isEditing: edit.isEditing,
                  editContent: edit.editContent,
                  originalContent: edit.originalContent,
                  showDiff: edit.showDiff,
                  editorRef: edit.editorRef,
                  onEditorChange: edit.handleEditorChange,
                  onUndoRedoChange: edit.handleUndoRedoChange,
                  onEditorTextSelect: handleEditorTextSelect,
                  onAddContext: onAddContext ? addViewerContext : null,
                  onContentMouseUp: handleContentMouseUp,
                  onViewerLink: handleViewerLink,
                  onAnchorLink: handleAnchorLink,
                }}
                onPageCount={onPageCount}
                canUpload={!readOnly}
                treeOpen={treeOpen}
                onShowTree={singleFileMode ? null : () => setTreeOpen(true)}
                onOpenChart={readOnly || singleFileMode ? null : () => { cancelPending(); tabs.openChart({ symbol: lastChartSymbol(workspaceId, { persist: persistStrip }) }); }}
              />
            </div>
          </div>

          <AnimatePresence initial={false}>
          {treeShown && (
            <TreeColumn
              key="tree"
              filter={filter}
              selection={selection}
              backup={backup}
              filesLoading={filesLoading}
              filesError={filesError}
              onRefreshFiles={onRefreshFiles}
              showSystemFiles={showSystemFiles}
              onToggleSystemFiles={onToggleSystemFiles}
              scopeDir={scopeDir}
              onClearScope={() => setScopeDir(null)}
              openPaths={tabs.openPaths}
              activePath={selectedFile}
              onFileClick={tree.onOpen}
              onFileDoubleClick={(path) => { tabs.openFile(path, { pin: true }); void cache.fetchBody(path).catch(() => {}); }}
              onOpenFromKeyboard={tree.onOpen}
              onEscape={() => (selection.selectMode ? selection.exitSelectMode() : setTreeOpen(!narrow))}
              memoedMap={memoedMap}
              memoedTitle={t('context.inMemo')}
              onAddContext={onAddContext}
              setContextMenu={setContextMenu}
              activeContextPath={contextMenu?.filePath ?? null}
              readOnly={readOnly}
              uploadDisabled={uploadProgress !== null}
              onUpload={() => fileInputRef.current?.click()}
              previews={previews.previews}
              onOpenPreview={openPreviewTab}
              onOpenMemory={stores ? openMemory : null}
              onOpenMemo={viewInMemo}
              onOpenSettings={!readOnly && !isFlashWorkspace ? openSettings : null}
              workspaceName={wsData?.name}
              filesRestoreIncomplete={filesRestoreIncomplete}
              overlay={narrow}
              onDismissOverlay={() => setTreeOpen(false)}
            />
          )}
          </AnimatePresence>
        </div>

      <input ref={fileInputRef} type="file" className="hidden" onChange={handleFileInputChange} />

      {contextMenu && (
        <FileContextMenu
          menu={contextMenu}
          onAction={handleContextMenuAction}
          onClose={() => setContextMenu(null)}
          canAddContext={!!onAddContext}
          memoState={memoMimeForName(contextMenu.filePath)
            ? (memoedMap.has(contextMenu.filePath) ? 'present' : 'absent')
            : null}
          canDownload={canDownload}
          downloadState={contextMenuDownloadState}
          selectedCount={selection.selectedPaths.has(contextMenu.filePath) ? selection.selectedPaths.size : 0}
        />
      )}

      {selectedFile && exportModalOpen && (
        <Suspense fallback={null}>
          <ExportPreviewModal
            open={exportModalOpen}
            onOpenChange={setExportModalOpen}
            content={fileContent ?? ''}
            fileName={selectedFile}
            workspaceId={workspaceId}
            readFileFullFn={readFileFullFn}
          />
        </Suspense>
      )}
      {selectedFile && memoEntry && memoStaleSandboxText !== null && (
        <MemoDiffModal
          open={memoDiffOpen}
          memoKey={memoEntry.key}
          fileName={selectedFile.split('/').pop() || selectedFile}
          sandboxText={memoStaleSandboxText}
          onClose={() => setMemoDiffOpen(false)}
        />
      )}
    </div>
    </RouteLeaveGuardContext.Provider>
  );
}

export default FilePanel;
export type { ContextPayload, PanelTarget } from './filePanel/types';
export { SYSTEM_DIR_PREFIXES } from './filePanel/fileMeta';
// eslint-disable-next-line react-refresh/only-export-components
export { categorizeFileError } from './filePanel/fileErrors';
export { FileErrorDisplay } from './filePanel/fileErrors';

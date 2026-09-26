import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { z } from 'zod';
import { INTERVALS } from '@/lib/bars/chartConstants';
import { readTypedTicker } from '@/lib/marketUtils';
import type { FileLocation } from '../../utils/fileLocation';
import { isValidUuid } from '../../utils/uuid';
import { PREVIEW_PORT_MAX, PREVIEW_PORT_MIN, type ChartTabSpec, type PlanTabSpec, type PreviewSpec, type ToolTabSpec } from './types';

/**
 * What one tab shows. The empty tab is the panel's landing surface and its
 * upload drop target.
 *
 * A file tab's `preview` is the VS Code rule: a single click in the tree lands
 * in the one italic tab, so browsing never piles up tabs, and anything that
 * says the file is being worked with (a double click, entering edit mode,
 * adding it to the context) pins it. It is unrelated to `kind: 'preview'`,
 * which is a running app served from the sandbox.
 *
 * A tool, plan or sources tab is on loan the same way, each kind in a slot of
 * its own: clicking through a turn's tool rows retargets one tool tab rather
 * than opening one per row, and never takes the file being browsed. These
 * three point into the transcript and are never stored: a tool or sources tab
 * carries an id and reads the live record at render, a plan tab carries the
 * one text a plan ever has.
 *
 * Settings, memory, memo and status are each the one tab of their kind, and
 * carry nothing: the body is the store itself, or the live watch. The first
 * three are kept across reloads like a file; status depends on a watch that
 * may be over by then, so it is not.
 */
type TabBody =
  | { kind: 'empty' }
  | {
    kind: 'file';
    path: string;
    preview: boolean;
    /** Where a reference pointed inside this file, kept so switching back returns there. */
    location: FileLocation | null;
    /** Bumped whenever a location arrives, so asking for the same line twice replays the highlight. */
    locationSeq: number;
  }
  | { kind: 'settings' }
  | { kind: 'memory' }
  | { kind: 'memo' }
  | { kind: 'status' }
  | {
    kind: 'preview';
    /** The sandbox port the dev server listens on. Identifies the tab. */
    port: number;
    /** What the agent called the app, when it said. */
    title?: string;
    /** A path suffix on the served app, e.g. `/timeline.html`. */
    previewPath?: string;
    /** How the agent started the server, so a restored tab can restart an idle port. */
    command?: string;
  }
  | { kind: 'chart'; symbol: string; timeframe: string; workspaceId?: string }
  | ({ kind: 'tool'; preview: boolean } & ToolTabSpec)
  | ({ kind: 'plan'; preview: boolean } & PlanTabSpec)
  | { kind: 'sources'; preview: boolean; messageId: string };

export type FileTab = { id: string } & TabBody;

/** The kinds that live only as long as the transcript, or the watch, they point into. */
const EPHEMERAL_KINDS = ['tool', 'plan', 'sources', 'status'] as const satisfies readonly FileTab['kind'][];
type EphemeralKind = (typeof EPHEMERAL_KINDS)[number];
type PersistableTab = Exclude<FileTab, { kind: 'empty' | EphemeralKind }>;

/** The kinds with one tab each, reopened by focusing it. */
type SingletonKind = 'settings' | 'memory' | 'memo' | 'status';

/** A tab the file tree can sit beside: the listing belongs to files and the empty tab, not to a chart, a tool result or an app. */
export function isListingTab(tab: FileTab): boolean {
  return tab.kind === 'file' || tab.kind === 'empty';
}

/** A tab that the next open of its kind may take over. */
export function isOnLoan(tab: FileTab): tab is Extract<FileTab, { preview: boolean }> {
  return (tab.kind === 'file' || tab.kind === 'tool' || tab.kind === 'plan' || tab.kind === 'sources') && tab.preview;
}

export interface OpenFileOptions {
  /** Open as a kept tab rather than the reused preview one. */
  pin?: boolean;
  location?: FileLocation | null;
}

interface TabsState {
  tabs: FileTab[];
  activeId: string;
  /**
   * The workspace and thread this strip belongs to. A thread switch queues the
   * new strip and re-runs the persist effect in the same flush, still holding
   * the old strip but already seeing the new key; without this the outgoing
   * thread's tabs would be written over the incoming thread's saved strip.
   */
  key: string | null;
}

/** A strip before it is tied to a key. */
type StripBody = Omit<TabsState, 'key'>;

const DEFAULT_TIMEFRAME = '1day';

/** The intervals the chart toolbar can actually show and request bars for. */
const INTERVAL_KEYS = new Set(INTERVALS.map(({ key }) => key));

// The stored strip is names only: a file tab is its path, a preview tab its
// port (the signed URL it runs on is short-lived and never stored), a chart
// its symbol. Unknown keys are dropped, so entries written before a field
// existed still load. A kind with no schema here (tool, plan, sources,
// status) is never written and, should one turn up, dropped on read: its
// body is a transcript record that a reload replays under a new identity, or
// a watch that has ended.
// One schema per kind, looked up by the entry's own `kind`, rather than a
// discriminated union: the union is the one zod feature nothing on the first
// load uses, and pulling it in charges the entry chunk for a lazy panel.
const persistedTabSchemas = {
  file: z.object({ kind: z.literal('file'), path: z.string().min(1), preview: z.boolean().catch(false) }),
  settings: z.object({ kind: z.literal('settings') }),
  memory: z.object({ kind: z.literal('memory') }),
  memo: z.object({ kind: z.literal('memo') }),
  preview: z.object({
    kind: z.literal('preview'),
    // Outside the served range the tab would restore as a permanent error
    // card rather than an app, so it is dropped with the entry.
    port: z.number().int().min(PREVIEW_PORT_MIN).max(PREVIEW_PORT_MAX),
    title: z.string().optional(),
    previewPath: z.string().optional(),
    command: z.string().optional(),
  }),
  chart: z.object({
    kind: z.literal('chart'),
    // A tab whose symbol is not a ticker is dropped like any other entry this
    // build cannot read; a blank one would open every request on a blank name.
    symbol: z.string().transform((v, ctx) => {
      const ticker = readTypedTicker(v);
      if (!ticker) { ctx.addIssue({ code: 'custom', message: 'not a ticker' }); return z.NEVER; }
      return ticker;
    }),
    // An interval this build does not carry has no toolbar label and asks the
    // bars endpoint for a bucket it does not serve, so a restored tab would
    // come back blank rather than on the daily view.
    timeframe: z.string().refine((v) => INTERVAL_KEYS.has(v)).catch(DEFAULT_TIMEFRAME),
    // A workspace gone or never an id would be asked for on every open.
    workspaceId: z.string().refine(isValidUuid).optional().catch(undefined),
  }),
};
type PersistedTabKind = keyof typeof persistedTabSchemas;
type PersistedTab = { [K in PersistedTabKind]: z.infer<(typeof persistedTabSchemas)[K]> }[PersistedTabKind];

function parsePersistedTab(entry: unknown): PersistedTab | null {
  const kind = typeof entry === 'object' && entry !== null ? (entry as { kind?: unknown }).kind : undefined;
  if (typeof kind !== 'string' || !Object.hasOwn(persistedTabSchemas, kind)) return null;
  const parsed = persistedTabSchemas[kind as PersistedTabKind].safeParse(entry);
  return parsed.success ? parsed.data : null;
}

/** More tabs than fit any strip is a corrupt or hostile store; the excess is dropped, not fatal. */
const MAX_PERSISTED_TABS = 64;

const persistedStateSchema = z.object({
  tabs: z.array(z.unknown()).transform((tabs) => tabs.slice(0, MAX_PERSISTED_TABS)),
  // -1 is the empty tab, which is not stored as an entry.
  active: z.number().int().catch(0),
});

/**
 * The workspace's strip: what the panel last showed there, in any thread. It is
 * the seed a thread with no strip of its own starts from, so a new conversation
 * about the same files opens on them rather than on nothing.
 */
export function tabsStorageKey(workspaceId: string): string {
  return `filePanel.tabs.${workspaceId}`;
}

/** A thread's own strip, which wins over the workspace seed once it exists. */
export function threadTabsStorageKey(workspaceId: string, threadId: string): string {
  return `filePanel.tabs.${workspaceId}.t.${threadId}`;
}

let nextId = 0;
const newId = () => `t${++nextId}`;

/** A broad index, for a chart opened before any symbol has been asked for. */
export const DEFAULT_CHART_SYMBOL = 'SPY';

/** Per workspace, like the strip: the last chart looked at is about that workspace's companies. */
export function lastChartStorageKey(workspaceId: string): string {
  return `filePanel.lastChart.${workspaceId}`;
}

/**
 * What a chart opened with no symbol shows: the last one looked at in this
 * workspace, else the default. Charts are switched in place, so the first
 * frame only has to be something worth looking at while the ticker is typed.
 * A strip that is not stored reads nothing, the same as it writes nothing.
 */
export function lastChartSymbol(workspaceId: string, { persist = true }: { persist?: boolean } = {}): string {
  if (!persist || !workspaceId) return DEFAULT_CHART_SYMBOL;
  try {
    return readTypedTicker(localStorage.getItem(lastChartStorageKey(workspaceId)) ?? '') ?? DEFAULT_CHART_SYMBOL;
  } catch {
    return DEFAULT_CHART_SYMBOL;
  }
}

function rememberChartSymbol(workspaceId: string, symbol: string): void {
  if (!workspaceId) return;
  try { localStorage.setItem(lastChartStorageKey(workspaceId), symbol); } catch { /* not worth failing over */ }
}

/**
 * The ticker as a tab knows it, or null for anything the persisted schema
 * would drop on reload, so a tab and its restored copy never disagree.
 */
function chartTicker(symbol: string): string | null {
  return readTypedTicker(symbol);
}

function emptyTab(): FileTab {
  return { id: newId(), kind: 'empty' };
}

function blankStrip(): StripBody {
  const tab = emptyTab();
  return { tabs: [tab], activeId: tab.id };
}

/** Strips written before tabs carried a `kind` held files only. */
function withKind(raw: unknown): unknown {
  return raw && typeof raw === 'object' && !('kind' in raw) ? { ...raw, kind: 'file' } : raw;
}

function revive(t: PersistedTab): FileTab {
  return t.kind === 'file' ? { id: newId(), ...t, location: null, locationSeq: 0 } : { id: newId(), ...t };
}

function toPersisted({ id: _id, ...body }: PersistableTab): PersistedTab {
  if (body.kind !== 'file') return body;
  const { location: _location, locationSeq: _seq, ...file } = body;
  return file;
}

/**
 * The strip a key holds, or null when it holds nothing readable. A key that
 * parses to no tabs is a strip the reader emptied on purpose, and comes back
 * as the empty tab rather than as "absent", which would reseed it.
 */
function readPersisted(key: string | null): StripBody | null {
  if (!key) return null;
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const saved = persistedStateSchema.safeParse(JSON.parse(raw));
    if (!saved.success) return null;
    // `active` indexes the saved array, entries this build drops included, so
    // it is matched while walking that array: the saved tab if it survived,
    // else the nearest surviving one before it.
    const tabs: FileTab[] = [];
    let activeId: string | null = null;
    saved.data.tabs.forEach((entry, i) => {
      const tab = parsePersistedTab(withKind(entry));
      if (!tab) return;
      const revived = revive(tab);
      tabs.push(revived);
      if (i <= saved.data.active) activeId = revived.id;
    });
    if (!tabs.length) return blankStrip();
    if (saved.data.active < 0) {
      const empty = emptyTab();
      return { tabs: [...tabs, empty], activeId: empty.id };
    }
    return { tabs, activeId: activeId ?? tabs[0].id };
  } catch {
    return null;
  }
}

function replaceIn(state: TabsState, id: string, body: TabBody): TabsState {
  return { ...state, tabs: state.tabs.map((t) => (t.id !== id ? t : { ...body, id })), activeId: id };
}

function patchIn(state: TabsState, id: string, fn: (tab: FileTab) => FileTab): TabsState {
  return { ...state, tabs: state.tabs.map((t) => (t.id === id ? fn(t) : t)) };
}

/**
 * Land a tab: the one that already matches is remade in place and brought to
 * the front; else an empty active tab is filled; else a new tab opens beside
 * the active one. With `loan` the active tab is also a target when it is the
 * on-loan tab of the opener's kind, and failing that whichever tab carries
 * that loan, so browsing never piles tabs up.
 */
function place(
  state: TabsState,
  match: (tab: FileTab) => boolean,
  make: (existing: FileTab | null) => TabBody,
  { loan }: { loan?: (tab: FileTab) => boolean } = {},
): TabsState {
  const existing = state.tabs.find(match);
  if (existing) return replaceIn(state, existing.id, make(existing));

  const onLoan = (t: FileTab) => !!loan && isOnLoan(t) && loan(t);
  const active = state.tabs.find((t) => t.id === state.activeId);
  const slot = active && (active.kind === 'empty' || onLoan(active)) ? active : state.tabs.find(onLoan);
  if (slot) return replaceIn(state, slot.id, make(null));

  const fresh: FileTab = { id: newId(), ...make(null) };
  const at = state.tabs.findIndex((t) => t.id === state.activeId);
  const tabs = [...state.tabs];
  tabs.splice(at < 0 ? tabs.length : at + 1, 0, fresh);
  return { ...state, tabs, activeId: fresh.id };
}

/** Closing the last tab leaves the empty one rather than a panel with no way back. */
function closeIn(state: TabsState, id: string): TabsState {
  const at = state.tabs.findIndex((t) => t.id === id);
  if (at < 0) return state;
  const tabs = state.tabs.filter((t) => t.id !== id);
  if (!tabs.length) return { ...blankStrip(), key: state.key };
  const activeId = id === state.activeId ? (tabs[at] ?? tabs[at - 1]).id : state.activeId;
  return { ...state, tabs, activeId };
}

/**
 * The panel's open tabs.
 *
 * Tabs persist per thread and come back as names only. Nothing is read until
 * a tab is activated, so reopening a thread with eight tabs costs one
 * request, not eight: the bodies live in the React Query cache, keyed by path,
 * and a tab is only a claim on one.
 *
 * A thread without a strip of its own inherits the workspace's: a new
 * conversation is usually about the files the last one left open. Until a
 * fresh chat has a thread id (its first message mints one) the strip is the
 * workspace's, and it carries over unchanged when the id arrives.
 *
 * `persist: false` keeps a strip in memory for the life of the mount: it reads
 * and writes nothing, including the last chart symbol. Its identity still
 * follows the real workspace, so it is rebuilt when the panel is pointed at
 * another one rather than showing the first workspace's tabs under the
 * second's id.
 *
 * `active: false` is a panel that is mounted but not on screen: it reads its
 * strip and keeps it, but writes nothing while hidden. The seed is the strip
 * shown last, and a hidden panel is not showing anything; a write from it
 * would put its tabs under the next conversation in place of the ones the
 * reader is looking at. It writes again once it is back on screen.
 */
export function useFileTabs(
  workspaceId: string,
  threadId?: string | null,
  { persist = true, active: onScreen = true }: { persist?: boolean; active?: boolean } = {},
) {
  // Which strip this is. No key for an adapter mount (a share has no workspace
  // of its own), so two shares cannot inherit each other's tab strip.
  const seedKey = workspaceId ? tabsStorageKey(workspaceId) : null;
  const key = workspaceId && threadId ? threadTabsStorageKey(workspaceId, threadId) : seedKey;

  // Where it is kept, which is nowhere unless it persists.
  const seedStore = persist ? seedKey : null;
  const store = persist ? key : null;

  const [state, setState] = useState<TabsState>(() => ({
    ...(readPersisted(store) ?? readPersisted(seedStore) ?? blankStrip()),
    key,
  }));

  // A thread switch is a different strip, not a reordering of this one. A
  // thread with nothing saved keeps what is on screen when the workspace is the
  // same (that is the seed, and keeping it in memory keeps its locations too);
  // a new workspace reads its own seed.
  const lastKey = useRef(key);
  const lastSeed = useRef(seedKey);
  useEffect(() => {
    if (lastKey.current === key) return;
    const sameWorkspace = lastSeed.current === seedKey;
    lastKey.current = key;
    lastSeed.current = seedKey;
    setState((prev) => ({
      ...(readPersisted(store) ?? (sameWorkspace ? prev : (readPersisted(seedStore) ?? blankStrip()))),
      key,
    }));
  }, [key, seedKey, store, seedStore]);

  // Written only once the strip on screen is the one this key names; the
  // render between a key change and the switch above still holds the old strip.
  useEffect(() => {
    if (!store || !onScreen || state.key !== key) return;
    const named = state.tabs.filter((t): t is PersistableTab => t.kind !== 'empty' && !(EPHEMERAL_KINDS as readonly string[]).includes(t.kind));
    // -1 when the empty tab is active, so the strip comes back parked on it.
    // A tab that is not stored comes back on the stored one before it, else
    // the first, else parked: what it showed is gone with the transcript.
    let active = named.findIndex((t) => t.id === state.activeId);
    if (active < 0 && named.length && !state.tabs.some((t) => t.id === state.activeId && t.kind === 'empty')) {
      const at = state.tabs.findIndex((t) => t.id === state.activeId);
      active = Math.max(0, named.filter((t) => state.tabs.indexOf(t) < at).length - 1);
    }
    const json = JSON.stringify({ tabs: named.map(toPersisted), active });
    try {
      localStorage.setItem(store, json);
      // The seed is whatever strip was shown last, in whichever thread.
      if (seedStore && seedStore !== store) localStorage.setItem(seedStore, json);
    } catch { /* a full or blocked store is not worth failing a render over */ }
  }, [state, key, store, seedStore, onScreen]);

  const activate = useCallback((id: string) => {
    setState((prev) => (prev.activeId === id ? prev : { ...prev, activeId: id }));
  }, []);

  const newTab = useCallback(() => {
    setState((prev) => {
      const tab = emptyTab();
      return { ...prev, tabs: [...prev.tabs, tab], activeId: tab.id };
    });
  }, []);

  /**
   * Bring a tab the tree can show beside to the front, for a folder asked for
   * with a chart or a tool result in front: the active tab if it already is
   * one, else the last file or empty tab in the strip, else a new empty tab.
   */
  const showListing = useCallback(() => {
    setState((prev) => {
      const active = prev.tabs.find((t) => t.id === prev.activeId);
      if (active && isListingTab(active)) return prev;
      const listing = prev.tabs.findLast(isListingTab);
      if (listing) return { ...prev, activeId: listing.id };
      const tab = emptyTab();
      return { ...prev, tabs: [...prev.tabs, tab], activeId: tab.id };
    });
  }, []);

  /** Apply `fn` to one tab; the tab's own kind decides what it means. */
  const patchTab = useCallback((id: string, fn: (tab: FileTab) => FileTab) => {
    setState((prev) => patchIn(prev, id, fn));
  }, []);

  /**
   * Open a file, or activate the tab already showing it. A pinned open never
   * lands on a loaned tab, so pinning one file and clicking another never
   * overwrites the first; re-opening a kept tab as a preview leaves it kept.
   */
  const openFile = useCallback((path: string, { pin = false, location = null }: OpenFileOptions = {}) => {
    setState((prev) => place(prev, (t) => t.kind === 'file' && t.path === path, (existing) => {
      const kept = existing?.kind === 'file' ? existing : null;
      return {
        kind: 'file',
        path,
        preview: kept ? (pin ? false : kept.preview) : !pin,
        location: location ?? kept?.location ?? null,
        locationSeq: (kept?.locationSeq ?? 0) + (location ? 1 : 0),
      };
    }, { loan: pin ? undefined : (t) => t.kind === 'file' }));
  }, []);

  /** A second open of a singleton kind comes back to the tab it already has. */
  const openSingleton = useCallback((kind: SingletonKind) => {
    setState((prev) => place(prev, (t) => t.kind === kind, () => ({ kind })));
  }, []);
  const openSettings = useCallback(() => openSingleton('settings'), [openSingleton]);
  const openMemory = useCallback(() => openSingleton('memory'), [openSingleton]);
  const openMemo = useCallback(() => openSingleton('memo'), [openSingleton]);
  /**
   * `focus: false` is for a watch the agent started: the tab joins the end of
   * the strip and the reader stays on what they were reading.
   */
  const openStatus = useCallback(({ focus = true }: { focus?: boolean } = {}) => {
    if (focus) return openSingleton('status');
    setState((prev) => (prev.tabs.some((t) => t.kind === 'status')
      ? prev
      : { ...prev, tabs: [...prev.tabs, { id: newId(), kind: 'status' }] }));
  }, [openSingleton]);

  /**
   * One tab per port: a second open of a running app comes back to the tab it
   * already has. A re-open that names nothing keeps the labelling the tab has,
   * so the tree's own click cannot blank a title the agent gave it.
   */
  const openPreview = useCallback(({ port, title, path, command }: PreviewSpec) => {
    setState((prev) => place(prev, (t) => t.kind === 'preview' && t.port === port, (existing) => ({
      ...(existing?.kind === 'preview' ? existing : { kind: 'preview', port }),
      ...(title !== undefined && { title }),
      ...(path !== undefined && { previewPath: path }),
      ...(command !== undefined && { command }),
    })));
  }, []);

  /**
   * One tab per symbol: asking for GOOGL again comes back to the GOOGL tab. A
   * re-open that names an interval moves the chart there; one that does not
   * leaves the tab on whatever interval it was last looked at on. The
   * workspace is the ask's each time, so the tab draws the annotations the
   * card that opened it was about.
   */
  const openChart = useCallback(({ symbol, timeframe, workspaceId: chartWorkspaceId }: ChartTabSpec) => {
    const ticker = chartTicker(symbol);
    if (!ticker) return;
    if (persist) rememberChartSymbol(workspaceId, ticker);
    setState((prev) => place(prev, (t) => t.kind === 'chart' && t.symbol === ticker, (existing) => ({
      kind: 'chart',
      symbol: ticker,
      timeframe: timeframe ?? (existing?.kind === 'chart' ? existing.timeframe : DEFAULT_TIMEFRAME),
      ...(chartWorkspaceId && { workspaceId: chartWorkspaceId }),
    })));
  }, [workspaceId, persist]);

  /**
   * Point a chart tab at another symbol, keeping its place in the strip and
   * its interval. If another tab already shows that symbol the two fold into
   * one, so the symbol rule holds without the user closing anything.
   */
  const retargetChart = useCallback((id: string, symbol: string) => {
    const ticker = chartTicker(symbol);
    if (!ticker) return;
    if (persist) rememberChartSymbol(workspaceId, ticker);
    setState((prev) => {
      const tab = prev.tabs.find((t) => t.id === id);
      if (tab?.kind !== 'chart') return prev;
      const other = prev.tabs.find((t) => t.kind === 'chart' && t.symbol === ticker && t.id !== id);
      if (other) {
        // The switch is the reader's, so the tab keeps its interval and the
        // drawings it was showing; the other tab's workspace goes with it.
        const folded = patchIn(closeIn(prev, id), other.id, (t) => {
          if (t.kind !== 'chart') return t;
          const { workspaceId: _dropped, ...rest } = t;
          return { ...rest, timeframe: tab.timeframe, ...(tab.workspaceId && { workspaceId: tab.workspaceId }) };
        });
        return { ...folded, activeId: other.id };
      }
      return patchIn(prev, id, (t) => (t.kind === 'chart' ? { ...t, symbol: ticker } : t));
    });
  }, [workspaceId, persist]);

  /**
   * One tab per tool call: the row clicked again comes back to its tab. A
   * first open lands on the loaned tool or plan tab, so reading through a
   * turn's rows never piles tabs up.
   */
  const openTool = useCallback(({ toolCallId }: ToolTabSpec) => {
    setState((prev) => place(prev, (t) => t.kind === 'tool' && t.toolCallId === toolCallId, (existing) => ({
      kind: 'tool',
      toolCallId,
      preview: existing?.kind === 'tool' ? existing.preview : true,
    }), { loan: (t) => t.kind === 'tool' || t.kind === 'plan' }));
  }, []);

  /** One tab per plan, sharing the tool slot: it is read the same way, between the rows it came from. */
  const openPlan = useCallback(({ planId, plan }: PlanTabSpec) => {
    setState((prev) => place(prev, (t) => t.kind === 'plan' && t.planId === planId, (existing) => ({
      kind: 'plan',
      planId,
      plan,
      preview: existing?.kind === 'plan' ? existing.preview : true,
    }), { loan: (t) => t.kind === 'tool' || t.kind === 'plan' }));
  }, []);

  /** One tab per turn; a first open lands on the loaned sources tab. */
  const openSources = useCallback((messageId: string) => {
    setState((prev) => place(prev, (t) => t.kind === 'sources' && t.messageId === messageId, (existing) => ({
      kind: 'sources',
      messageId,
      preview: existing?.kind === 'sources' ? existing.preview : true,
    }), { loan: (t) => t.kind === 'sources' }));
  }, []);

  const pinTab = useCallback((id: string) => {
    patchTab(id, (t) => (isOnLoan(t) ? { ...t, preview: false } : t));
  }, [patchTab]);

  const closeTab = useCallback((id: string) => setState((prev) => closeIn(prev, id)), []);

  /** Drop a tab's location: the focus chip's dismiss, which must survive a tab switch. */
  const clearLocation = useCallback((id: string) => {
    patchTab(id, (t) => (t.kind === 'file' ? { ...t, location: null } : t));
  }, [patchTab]);

  const activeTab = state.tabs.find((t) => t.id === state.activeId) ?? state.tabs[0];
  const openPaths = useMemo(
    () => new Set(state.tabs.flatMap((t) => (t.kind === 'file' ? [t.path] : []))),
    [state.tabs],
  );

  // One object per strip change: the api lands in effect deps and memo deps
  // downstream, and each member is already stable on its own.
  return useMemo(() => ({
    tabs: state.tabs,
    activeTab,
    activeId: activeTab.id,
    openPaths,
    activate,
    openFile,
    pinTab,
    closeTab,
    newTab,
    showListing,
    openSettings,
    openMemory,
    openMemo,
    openStatus,
    openPreview,
    openChart,
    retargetChart,
    openTool,
    openPlan,
    openSources,
    patchTab,
    clearLocation,
  }), [
    state.tabs, activeTab, openPaths,
    activate, openFile, pinTab, closeTab, newTab, showListing, openSettings, openMemory, openMemo, openStatus,
    openPreview, openChart, retargetChart, openTool, openPlan, openSources, patchTab, clearLocation,
  ]);
}

export type FileTabsApi = ReturnType<typeof useFileTabs>;

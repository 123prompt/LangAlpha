/**
 * Bridge to the Electron desktop shell, injected by its preload script.
 *
 * Undefined in every browser build, so treat desktop as an enhancement and keep
 * the browser path alive at each call site. The shell updates on its own slow
 * cadence while this app deploys continuously, so a new web build must never
 * require a new shell: feature-detect each method, never the version.
 *
 * Nothing here touches auth. The shell intercepts the OAuth authorize navigation
 * itself and hands the code back through this app's own /callback route, so
 * sign-in needs no desktop-specific code on this side.
 */
/** What the shell will accept for a PDF export; everything is optional. */
export interface SavePdfOptions {
  /** Suggested filename. The shell sanitizes it and appends `.pdf`. */
  fileName?: string;
  landscape?: boolean;
  /** 0.1 to 2. Clamped by the shell rather than rejected. */
  scale?: number;
  pageSize?:
    | 'A0' | 'A1' | 'A2' | 'A3' | 'A4' | 'A5' | 'A6'
    | 'Legal' | 'Letter' | 'Tabloid' | 'Ledger';
  /** Comma-separated 1-based pages or ranges, e.g. `1-3, 7`. */
  pageRanges?: string;
  /** Defaults to true; false drops backgrounds the way a browser does. */
  printBackground?: boolean;
  /** Tagged reading order, defaults to true. */
  tagged?: boolean;
  /** PDF outline built from the heading tree, defaults to true. */
  outline?: boolean;
}

/**
 * Deliberately three outcomes, not a boolean. A caller falls back to browser
 * print when the method is absent, but must NOT fall back on `canceled`:
 * reopening a print dialog the user just dismissed is the one response that
 * reads as the app ignoring them.
 */
export type SavePdfResult =
  | { saved: true }
  | { canceled: true }
  | { error: string };

export interface DesktopBridge {
  readonly version: string;
  /** Node's process.platform: 'darwin' | 'win32' | 'linux'. */
  readonly platform: string;
  /**
   * Whether the shell hid this window's titlebar. Absent before shell 0.1.1.
   *
   * Per window, not per platform: the same install opens a frameless main
   * window and a framed account window, and a first launch is framed on macOS
   * too. Only the shell knows.
   */
  readonly windowChrome?: 'hidden' | 'native';
  /** Tells the shell which theme the page settled on. Added in shell 0.1.0. */
  setTheme?(theme: 'light' | 'dark'): void;
  openExternal?(url: string): Promise<void>;
  /**
   * Render this page to a PDF and let the user choose where it lands, with no
   * print dialog in between. Added in shell 0.1.2; feature-detect it.
   */
  savePdf?(options?: SavePdfOptions): Promise<SavePdfResult>;
}

declare global {
  interface Window {
    langalphaDesktop?: DesktopBridge;
  }
}

export const desktop: DesktopBridge | undefined =
  typeof window === 'undefined' ? undefined : window.langalphaDesktop;

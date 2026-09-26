import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import type { ReactElement } from 'react';

import HtmlViewer from '../../HtmlViewer';
import { ThemeProvider, useTheme } from '@/contexts/ThemeContext';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

const toastMock = vi.fn();
vi.mock('@/components/ui/use-toast', () => ({
  toast: (...args: unknown[]) => toastMock(...args),
}));

// Avoid pulling the heavy prism-async highlighter into jsdom. The style prop is
// surfaced as data-palette so theme-reactivity can be asserted. (Objects are
// inlined in the factory — vi.mock is hoisted above any top-level consts.)
vi.mock('../../../SyntaxHighlighter', () => ({
  default: ({ children, style }: { children: string; style?: { __palette?: string } }) => (
    <pre data-testid="syntax-highlighter" data-palette={style?.__palette}>
      {children}
    </pre>
  ),
  oneDark: { __palette: 'dark' },
  oneLight: { __palette: 'light' },
}));

// The owner's served URL rides a grant and "open in new tab" opens the file's
// own /a/ page; both come from hooks the viewer reads, mocked here so the
// test needs no query client. A test can take the grant away or fail it.
const grant = vi.hoisted(() => ({
  state: { minted: true, error: null as Error | null },
  refetch: vi.fn(),
}));
vi.mock('@/hooks/useWorkspaceFileGrant', () => ({
  useWorkspaceFileGrant: (workspaceId: string | null) => ({
    data: workspaceId && grant.state.minted
      ? { prefix: '/api/v1/wsfiles/g/grant-1/', expires_in: 12 * 60 * 60 }
      : undefined,
    error: workspaceId ? grant.state.error : null,
    refetch: grant.refetch,
  }),
}));
vi.mock('@/hooks/useShareLink', () => ({
  useShareLink: (workspaceId: string | null) => ({
    data: workspaceId ? { code: 'abc123abc123' } : undefined,
  }),
}));

const defaultProps = {
  content: '<!DOCTYPE html><html><body><h1>Report</h1></body></html>',
  fileName: 'report.html',
  workspaceId: 'ws-1',
  filePath: 'results/report.html',
  onTriggerDownload: vi.fn(),
};

function getPreviewIframe(): HTMLIFrameElement {
  // Preview iframe is the served one; fullscreen iframe is portaled separately.
  const frame = document.querySelector('iframe.html-viewer-frame');
  return frame as HTMLIFrameElement;
}

// HtmlViewer consumes ThemeContext; render under a provider with a control that
// flips the resolved theme so reactivity can be driven the way the app does.
function ThemeToggle() {
  const { setTheme } = useTheme();
  return (
    <>
      <button onClick={() => setTheme('light')}>set-light</button>
      <button onClick={() => setTheme('dark')}>set-dark</button>
    </>
  );
}

function renderViewer(ui: ReactElement) {
  return render(
    <ThemeProvider>
      <ThemeToggle />
      {ui}
    </ThemeProvider>,
  );
}

describe('HtmlViewer', () => {
  beforeEach(() => {
    toastMock.mockClear();
    localStorage.clear();
    grant.state.minted = true;
    grant.state.error = null;
    grant.refetch.mockClear();
  });

  it('renders the Preview iframe pointed at the wsfiles served URL with ?inject=theme', () => {
    renderViewer(<HtmlViewer {...defaultProps} />);
    const iframe = getPreviewIframe();
    expect(iframe).toBeTruthy();
    expect(iframe.getAttribute('src')).toBe(
      '/api/v1/wsfiles/g/grant-1/results/report.html?inject=theme',
    );
  });

  it('sandboxes the preview iframe without allow-same-origin', () => {
    renderViewer(<HtmlViewer {...defaultProps} />);
    expect(getPreviewIframe().getAttribute('sandbox')).toBe(
      'allow-scripts allow-popups allow-popups-to-escape-sandbox',
    );
  });

  it('switches to the Source tab and renders the full content in the highlighter', () => {
    renderViewer(<HtmlViewer {...defaultProps} />);
    fireEvent.click(screen.getByText('filePanel.htmlSource'));
    const highlighter = screen.getByTestId('syntax-highlighter');
    expect(highlighter).toBeInTheDocument();
    expect(highlighter).toHaveTextContent('<h1>Report</h1>');
  });

  // A reference into a report asks for a place in the rendered document, and
  // the Source tab cannot show one. The tab outlives both the file and the
  // anchor, so without this the click left the reader on the same markup with
  // nothing to say it had landed.
  describe('an anchored open', () => {
    const rerenderWith = (
      rerender: (ui: ReactElement) => void,
      props: { anchor?: string | null; anchorSeq?: number | null; filePath?: string },
    ) => rerender(
      <ThemeProvider>
        <ThemeToggle />
        <HtmlViewer {...defaultProps} {...props} />
      </ThemeProvider>,
    );

    it('brings Preview back when a reference arrives while Source is open', () => {
      const { rerender } = renderViewer(<HtmlViewer {...defaultProps} />);
      fireEvent.click(screen.getByText('filePanel.htmlSource'));
      expect(getPreviewIframe()).toBeFalsy();

      rerenderWith(rerender, { anchor: 'risks', anchorSeq: 1 });
      const iframe = getPreviewIframe();
      expect(iframe).toBeTruthy();
      expect(iframe.getAttribute('src')).toContain('#risks');
    });

    it('counts the same section asked for twice as two requests', () => {
      const { rerender } = renderViewer(
        <HtmlViewer {...defaultProps} anchor="risks" anchorSeq={1} />,
      );
      fireEvent.click(screen.getByText('filePanel.htmlSource'));
      expect(getPreviewIframe()).toBeFalsy();

      rerenderWith(rerender, { anchor: 'risks', anchorSeq: 2 });
      expect(getPreviewIframe()).toBeTruthy();
    });

    it('leaves Source alone while the same request is still in effect', () => {
      // The control: the switch follows a request, not the mere presence of an
      // anchor, so a deliberate move to Source is not undone on the next render.
      const { rerender } = renderViewer(
        <HtmlViewer {...defaultProps} anchor="risks" anchorSeq={1} />,
      );
      fireEvent.click(screen.getByText('filePanel.htmlSource'));
      rerenderWith(rerender, { anchor: 'risks', anchorSeq: 1 });
      expect(getPreviewIframe()).toBeFalsy();
      expect(screen.getByTestId('syntax-highlighter')).toBeInTheDocument();
    });

    it('asks the document to scroll when the same section is referenced again', () => {
      const { rerender } = renderViewer(
        <HtmlViewer {...defaultProps} anchor="risks" anchorSeq={1} />,
      );
      const iframe = getPreviewIframe();
      const postMessage = vi.fn();
      Object.defineProperty(iframe, 'contentWindow', {
        value: { postMessage },
        configurable: true,
      });

      rerenderWith(rerender, { anchor: 'risks', anchorSeq: 2 });

      // The URL is the one already loaded, so the browser navigates nowhere and
      // the fragment cannot land this open. Without the request the second
      // click moves nothing on screen.
      expect(iframe.getAttribute('src')).toBe(
        '/api/v1/wsfiles/g/grant-1/results/report.html?inject=theme#risks',
      );
      expect(postMessage).toHaveBeenCalledWith({ type: 'widget:scrollTo', id: 'risks' }, '*');
    });

    it('leaves Source alone when another file opens with no reference', () => {
      const { rerender } = renderViewer(
        <HtmlViewer {...defaultProps} anchor="risks" anchorSeq={1} />,
      );
      fireEvent.click(screen.getByText('filePanel.htmlSource'));
      rerenderWith(rerender, { anchor: null, anchorSeq: null, filePath: 'results/other.html' });
      expect(getPreviewIframe()).toBeFalsy();
    });
  });

  it('re-themes the Source highlighter when the app theme toggles', () => {
    renderViewer(<HtmlViewer {...defaultProps} />);
    fireEvent.click(screen.getByText('set-dark'));
    fireEvent.click(screen.getByText('filePanel.htmlSource'));
    expect(screen.getByTestId('syntax-highlighter')).toHaveAttribute('data-palette', 'dark');

    // Switching the app theme must re-theme the highlighter live, with no
    // tab/file remount — the palette is driven by the ThemeContext value, so a
    // context change re-renders the Source tab in place.
    fireEvent.click(screen.getByText('set-light'));
    expect(screen.getByTestId('syntax-highlighter')).toHaveAttribute('data-palette', 'light');

    fireEvent.click(screen.getByText('set-dark'));
    expect(screen.getByTestId('syntax-highlighter')).toHaveAttribute('data-palette', 'dark');
  });

  it('renders only view actions in the toolbar (fullscreen, open-in-new-tab)', () => {
    renderViewer(<HtmlViewer {...defaultProps} />);
    expect(screen.getByLabelText('filePanel.fullscreen')).toBeInTheDocument();
    expect(screen.getByLabelText('filePanel.openInNewTab')).toBeInTheDocument();
    // Download/PDF live in the file panel header's download menu, not here.
    expect(screen.queryByLabelText('filePanel.moreActions')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('filePanel.downloadAsHtml')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('filePanel.saveAsPdf')).not.toBeInTheDocument();
  });

  it('opens the fullscreen dialog hosting a served iframe', () => {
    renderViewer(<HtmlViewer {...defaultProps} />);
    fireEvent.click(screen.getByLabelText('filePanel.fullscreen'));
    // Dialog portals to body; its iframe also points at the served URL.
    const frames = Array.from(document.querySelectorAll('iframe.html-fullscreen-frame'));
    expect(frames).toHaveLength(1);
    expect((frames[0] as HTMLIFrameElement).getAttribute('src')).toBe(
      '/api/v1/wsfiles/g/grant-1/results/report.html?inject=theme',
    );
  });

  it('keeps fullscreen off until the served URL exists', () => {
    grant.state.minted = false;
    renderViewer(<HtmlViewer {...defaultProps} />);
    expect(screen.getByLabelText('filePanel.fullscreen')).toBeDisabled();
    expect(getPreviewIframe()).toBeFalsy();
  });

  // A grant that never arrived would otherwise leave a spinner up forever.
  it('shows an error with a retry when the grant cannot be minted', () => {
    grant.state.minted = false;
    grant.state.error = new Error('Request failed with status code 500');
    renderViewer(<HtmlViewer {...defaultProps} />);
    expect(screen.getByRole('alert')).toHaveTextContent('filePanel.htmlPreviewFailed');
    fireEvent.click(screen.getByText('common.retry'));
    expect(grant.refetch).toHaveBeenCalledTimes(1);
  });

  // A failed renewal keeps the grant the frame is already loading under.
  it('keeps the preview when a renewal fails behind a grant it already has', () => {
    grant.state.error = new Error('Request failed with status code 500');
    renderViewer(<HtmlViewer {...defaultProps} />);
    expect(getPreviewIframe()).toBeTruthy();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('serves the file under the share page\'s prefix, themed', () => {
    renderViewer(
      <HtmlViewer {...defaultProps} servePrefix="/api/v1/public/shared/tok-1/files/serve/" />,
    );
    expect(getPreviewIframe().getAttribute('src')).toBe(
      '/api/v1/public/shared/tok-1/files/serve/results/report.html?inject=theme',
    );
  });

  it("opens the file's own share page in a new tab, never the grant URL (owner view)", () => {
    const open = vi.fn();
    vi.stubGlobal('open', open);
    try {
      renderViewer(<HtmlViewer {...defaultProps} />);
      fireEvent.click(screen.getByLabelText('filePanel.openInNewTab'));
      expect(open).toHaveBeenCalledWith(
        `${window.location.origin}/a/abc123abc123`,
        '_blank',
        'noopener,noreferrer',
      );
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('opens the byte-faithful served URL on the share page', () => {
    const open = vi.fn();
    vi.stubGlobal('open', open);
    try {
      renderViewer(
        <HtmlViewer {...defaultProps} servePrefix="/api/v1/public/shared/tok-1/files/serve/" />,
      );
      fireEvent.click(screen.getByLabelText('filePanel.openInNewTab'));
      expect(open).toHaveBeenCalledWith(
        '/api/v1/public/shared/tok-1/files/serve/results/report.html',
        '_blank',
        'noopener,noreferrer',
      );
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

/**
 * A relative link in agent output is a workspace file. Left to the browser it
 * opens the app itself in a new tab, which is what "I click and no file opens"
 * looked like for any name outside the old extension allowlist.
 */
import { describe, it, expect, vi } from 'vitest';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

vi.mock('@/contexts/ThemeContext', () => ({
  useTheme: () => ({ theme: 'dark', setTheme: () => {} }),
}));

import Markdown from '../Markdown';

function render(content: string, variant: 'chat' | 'panel' = 'chat'): string {
  return renderToStaticMarkup(<Markdown variant={variant} content={content} onOpenFile={() => {}} />);
}

describe('Markdown file links', () => {
  it.each([
    ['a deck', '[deck](results/deck.pptx)'],
    ['a macro workbook', '[model](results/model.xlsm)'],
    ['an extensionless file', '[notes](results/NOTES)'],
    ['a name with spaces', '[deck](results/Q3 deck.pptx)'],
  ])('renders %s as an in-app file link', (_label, content) => {
    const html = render(content);
    expect(html).toContain('<a class="underline hover:opacity-80');
    expect(html).not.toContain('target="_blank"');
  });

  it('renders a bare name with a line suffix as a link', () => {
    expect(render('[model](model.py:42)')).toContain('<a class="underline hover:opacity-80');
  });

  it('renders a bare name with a line suffix and a title as a link', () => {
    // The anchor is what proves it: without the `./`, react-markdown's
    // `defaultUrlTransform` reads `model.py:` as a scheme and emits an <a>
    // with a title and no href at all, which renders as unclickable text.
    // Without the `./`, react-markdown's `defaultUrlTransform` reads `model.py:`
    // as a scheme and emits no href, so the link falls through to the web-link
    // branch: a new tab with nothing to open, rather than the panel.
    const html = render('[model](model.py:42 "source")');
    expect(html).toContain('cursor-pointer');
    expect(html).not.toContain('target="_blank"');
    expect(html).toContain('title="source"');
  });

  it('makes links inside a file viewed in the panel clickable', () => {
    const html = render('[appendix](appendix)', 'panel');
    expect(html).toContain('cursor-pointer');
    expect(html).not.toContain('target="_blank"');
  });

  it('keeps web links opening in a new tab', () => {
    expect(render('[site](https://example.com/report.md)')).toContain('target="_blank"');
  });
});

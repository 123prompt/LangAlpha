import { describe, it, expect } from 'vitest';
import { mapOutsideCode, mapOutsideFences } from '../markdownSegments';

const upper = (s: string): string => s.toUpperCase();

describe('mapOutsideCode', () => {
  it('reproduces the input exactly under an identity transform', () => {
    const doc = [
      'intro `code` text',
      '',
      '```json',
      '{ "a": 1 }',
      '```',
      '',
      'tail ~~~ not a fence',
      '~~~~',
      'tilde fenced',
      '~~~~',
      '',
      'trailing prose',
    ].join('\n');
    expect(mapOutsideCode(doc, (s) => s)).toBe(doc);
  });

  it('leaves fenced blocks untouched', () => {
    const doc = 'before\n```\ninside\n```\nafter';
    expect(mapOutsideCode(doc, upper)).toBe('BEFORE\n```\ninside\n```\nAFTER');
  });

  it('leaves inline code untouched', () => {
    expect(mapOutsideCode('a `b` c', upper)).toBe('A `b` C');
  });

  it('treats an unmatched backtick run as prose', () => {
    expect(mapOutsideCode('a ` b', upper)).toBe('A ` B');
  });

  it('protects an unterminated fence to the end of the document', () => {
    expect(mapOutsideCode('intro\n```\nstill code\nmore', upper)).toBe(
      'INTRO\n```\nstill code\nmore'
    );
  });

  it('requires a closing fence at least as long as the opener', () => {
    expect(mapOutsideCode('````\n```\nstill inside\n````\nout', upper)).toBe(
      '````\n```\nstill inside\n````\nOUT'
    );
  });

  it('honours an indented fence', () => {
    expect(mapOutsideCode('a\n   ```\nb\n   ```\nc', upper)).toBe(
      'A\n   ```\nb\n   ```\nC'
    );
  });

  it('does not carry a transform across a code boundary', () => {
    // A currency guard would otherwise pair a `$` before the fence with digits
    // after it; each prose span is transformed on its own.
    const doc = 'cost $\n```\n5\n```\n';
    expect(mapOutsideCode(doc, (s) => s.replace(/\$(?=[\s\S]*\d)/g, 'USD'))).toBe(doc);
  });
});

describe('mapOutsideFences', () => {
  it('still transforms inline code, which cannot span lines', () => {
    expect(mapOutsideFences('a `b` c', upper)).toBe('A `B` C');
  });

  it('leaves fenced blocks untouched', () => {
    expect(mapOutsideFences('a\n```\nb\n```\nc', upper)).toBe('A\n```\nb\n```\nC');
  });
});

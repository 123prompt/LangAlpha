import { afterAll, beforeAll, describe, it, expect } from 'vitest';
import i18n from '@/i18n';
import { formatDuration } from '../time';

const START = '2026-09-25T14:00:00.000Z';
const after = (ms: number) => new Date(Date.parse(START) + ms).toISOString();

describe('formatDuration', () => {
  it('reads a sub-second run as under a second, never in milliseconds', () => {
    expect(formatDuration(START, after(109))).toBe('<1s');
  });

  it('keeps whole seconds at every scale', () => {
    expect(formatDuration(START, after(47_000))).toBe('47s');
    expect(formatDuration(START, after(12 * 60_000 + 17_000))).toBe('12m 17s');
    expect(formatDuration(START, after(63 * 60_000))).toBe('1h 3m');
  });

  it('drops a zero tail, since a finished run never ticks', () => {
    expect(formatDuration(START, after(15 * 60_000))).toBe('15m');
    expect(formatDuration(START, after(120 * 60_000))).toBe('2h');
    expect(formatDuration(START, after(15 * 60_000 + 400))).toBe('15m');
  });

  it('shows a dash when either end is missing', () => {
    expect(formatDuration(START, null)).toBe('—');
    expect(formatDuration(undefined, START)).toBe('—');
  });

  describe('in Chinese', () => {
    beforeAll(() => i18n.changeLanguage('zh-CN'));
    afterAll(() => i18n.changeLanguage('en-US'));

    it('words the units in the reader\'s language', () => {
      expect(formatDuration(START, after(109))).toBe('不到 1 秒');
      expect(formatDuration(START, after(12 * 60_000 + 17_000))).toBe('12 分 17 秒');
      expect(formatDuration(START, after(15 * 60_000))).toBe('15 分钟');
    });
  });
});

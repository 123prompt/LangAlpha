import { describe, it, expect } from 'vitest';
import { formatUtcOffset, utcOffsetMinutes } from '../timezones';

const label = (tz: string, at: Date) => formatUtcOffset(utcOffsetMinutes(tz, at));

describe('formatUtcOffset', () => {
  const summer = new Date(Date.UTC(2025, 6, 2, 12, 0));
  const winter = new Date(Date.UTC(2025, 0, 15, 12, 0));

  it('labels whole-hour offsets without minutes', () => {
    expect(label('Asia/Hong_Kong', summer)).toBe('UTC+8');
    expect(label('Asia/Hong_Kong', winter)).toBe('UTC+8'); // no DST
  });

  it('is DST-aware, with a true minus sign', () => {
    expect(label('America/New_York', summer)).toBe('UTC−4');
    expect(label('America/New_York', winter)).toBe('UTC−5');
    expect(label('Europe/London', summer)).toBe('UTC+1');
  });

  it('reads a zero offset as plain UTC', () => {
    expect(label('Europe/London', winter)).toBe('UTC');
    expect(label('UTC', summer)).toBe('UTC');
  });

  it('keeps minutes for half-hour zones', () => {
    expect(label('Asia/Kolkata', summer)).toBe('UTC+5:30');
    expect(formatUtcOffset(-210)).toBe('UTC−3:30');
  });
});

import i18n from '@/i18n';
import { createDateFormatter } from '@/lib/format';
import { formatTook } from '@/lib/elapsed';

// Locale-keyed through lib/format: a component that renders these must also
// call useTranslation() so a locale switch re-renders it.
const dateTime = createDateFormatter({ month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit' });
const clock = createDateFormatter({ hour: 'numeric', minute: '2-digit' });

/** A formatter that reads the reader's own clock, or the clock of the zone
 *  it is given: a one-time run shows in the zone it was set in. */
function zonedFormatter(opts: Intl.DateTimeFormatOptions) {
  const byZone = new Map<string, (d: Date) => string>();
  return (d: Date, timeZone?: string): string => {
    const key = timeZone ?? '';
    let format = byZone.get(key);
    if (!format) {
      format = createDateFormatter(timeZone ? { ...opts, timeZone } : opts);
      byZone.set(key, format);
    }
    return format(d);
  };
}

const dateTimeShort = zonedFormatter({ month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
const weekdayShort = zonedFormatter({ weekday: 'short' });
const zonedClock = zonedFormatter({ hour: 'numeric', minute: '2-digit' });
// Composed, not one Intl pattern: with a weekday, Chinese switches to a
// two-digit hour ("周五07:00") where every other time here reads "7:00".
const weekdayClock = (d: Date, timeZone?: string) => `${weekdayShort(d, timeZone)} ${zonedClock(d, timeZone)}`;
const dayHeading = createDateFormatter({ weekday: 'long', month: 'short', day: 'numeric' });
const weekdayInitial = createDateFormatter({ weekday: 'narrow' });
const moment = zonedFormatter({ weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });

type DateInput = string | Date | null | undefined;

/** A server timestamp or a picked moment as a Date, or null for nothing or
 *  for a string that does not parse. */
export function toDate(d: DateInput): Date | null {
  if (!d) return null;
  const date = typeof d === 'string' ? new Date(d) : d;
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatDateTime(date: DateInput): string {
  const d = toDate(date);
  return d ? dateTime(d) : '—';
}

/** "Sep 24, 3:05 PM": the year is noise for anything this close to now. */
export function formatDateTimeShort(date: DateInput, timeZone?: string): string {
  const d = toDate(date);
  return d ? dateTimeShort(d, timeZone) : '';
}

export function formatClock(date: DateInput): string {
  const d = toDate(date);
  return d ? clock(d) : '';
}

/** A time within the coming week reads by weekday ("Fri 7:00 AM"); anything
 *  further out needs its date. */
export function formatUpcoming(date: DateInput, timeZone?: string): string {
  const d = toDate(date);
  if (!d) return '';
  return d.getTime() - Date.now() < 6 * 86_400_000 ? weekdayClock(d, timeZone) : dateTimeShort(d, timeZone);
}

export function formatDayHeading(date: Date): string {
  return dayHeading(date);
}

/** "Wed, Oct 28, 2:15 PM": one moment, with the weekday that makes it
 *  easy to place. */
export function formatMoment(date: Date, timeZone?: string): string {
  return moment(date, timeZone);
}

// 2024-01-01 was a Monday.
const WEEK = Array.from({ length: 7 }, (_, i) => new Date(2024, 0, 1 + i));

/** The seven weekday initials, Monday first, in the reader's locale. */
export function weekdayInitials(): string[] {
  return WEEK.map((d) => weekdayInitial(d));
}

/** The seven short weekday names ("Mon"), Monday first, in the reader's locale. */
export function weekdayNames(): string[] {
  return WEEK.map((d) => weekdayShort(d));
}

/** A wall-clock time with no date attached, in the reader's clock format. */
export function formatTimeOfDay(hour: number, minute: number): string {
  return clock(new Date(2024, 0, 1, hour, minute));
}

/** Local calendar day, for grouping a feed by the day the reader lived it. */
export function localDayKey(date: DateInput): string {
  const d = toDate(date);
  if (!d) return '';
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

export function durationMs(startDate: DateInput, endDate: DateInput): number | null {
  const start = toDate(startDate);
  const end = toDate(endDate);
  if (!start || !end) return null;
  return end.getTime() - start.getTime();
}

/** How long a run took, worded the way the chat words a turn ("12m 17s",
 *  "12 分 17 秒"): whole seconds, and "<1s" for a run that took less. */
export function formatDuration(startDate: DateInput, endDate: DateInput): string {
  const ms = durationMs(startDate, endDate);
  return ms == null ? '—' : formatTook(ms, (key, opts) => i18n.t(key, opts));
}

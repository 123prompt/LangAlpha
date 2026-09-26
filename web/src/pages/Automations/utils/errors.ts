import { apiErrorDetail, apiErrorDetailMessage } from '@/pages/ChatAgent/utils/api/errors';

/**
 * What a failed write says to the reader. A 422 carries FastAPI's list of
 * `{ loc, msg }`, which as a toast would read "[object Object]"; each entry
 * becomes the field it names and pydantic's sentence, without the `body`
 * prefix and the "Value error, " a model validator puts before its own text.
 */
export function mutationErrorMessage(err: unknown, fallback: string): string {
  const detail = apiErrorDetail(err);
  if (Array.isArray(detail)) {
    const parts = detail.flatMap((entry) => {
      const e = entry as { loc?: unknown; msg?: unknown };
      if (typeof e?.msg !== 'string' || !e.msg) return [];
      const msg = e.msg.replace(/^Value error, /, '');
      const path = Array.isArray(e.loc) ? e.loc.filter((p): p is string => typeof p === 'string' && p !== 'body') : [];
      const field = path[path.length - 1];
      return [field ? `${field}: ${msg}` : msg];
    });
    if (parts.length) return parts.join('; ');
  }
  if (typeof detail === 'string' && detail) return detail;
  const message = apiErrorDetailMessage(err) ?? (err as { message?: unknown })?.message;
  return typeof message === 'string' && message ? message : fallback;
}

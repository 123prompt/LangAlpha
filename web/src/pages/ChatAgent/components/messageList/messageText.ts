import type { ContentSegmentRecord } from './types';

/** The reply's prose in transcript order. Segments can land out of order, so they are sorted by `order` the way the bubble renders them; a message with no text segments falls back to its flat `content`. Shared by copy, the minimap preview and the deliverables deck so all three read the same text. */
export function assistantText(message: { contentSegments?: unknown; content?: unknown }): string {
  const segments = message.contentSegments as ContentSegmentRecord[] | undefined;
  const text = segments
    ?.filter((s) => s.type === 'text')
    .sort((a, b) => a.order - b.order)
    .map((s) => s.content ?? '')
    .join('');
  return text || (typeof message.content === 'string' ? message.content : '');
}

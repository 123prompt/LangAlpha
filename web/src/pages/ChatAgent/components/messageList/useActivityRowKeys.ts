import { useState } from 'react';
import type { ActivityItem } from './activityTypes';

interface RowKeys {
  items: ActivityItem[];
  preparing: boolean;
  nextSlot: number;
  slot: string;
  byId: Map<string, string>;
}

/** Argument chunks can precede a tool-call ID. Bind their placeholder to the
 * first new call so its wrapper continues instead of exiting over its successor. */
export function useActivityRowKeys(items: ActivityItem[], preparing: boolean) {
  const [previous, setPrevious] = useState<RowKeys>(() => ({
    items, preparing, nextSlot: 1, slot: 'preparing-0', byId: new Map(),
  }));
  let current = previous;
  const identitiesChanged = items.length !== previous.items.length
    || items.some((item, index) => item.id !== previous.items[index].id);
  if (identitiesChanged || preparing !== previous.preparing) {
    const byId = new Map(previous.byId);
    const known = new Set(previous.items.map((item) => item.id));
    const landed = previous.preparing && items.find((item) => item.type === 'tool_call' && !known.has(item.id));
    if (landed) byId.set(landed.id, previous.slot);
    const newSlot = preparing && (!previous.preparing || !!landed);
    current = { items, preparing, byId,
      nextSlot: previous.nextSlot + Number(newSlot),
      slot: newSlot ? `preparing-${previous.nextSlot}` : previous.slot };
    setPrevious(current);
  }
  return {
    preparing: current.slot,
    forItem: (item: ActivityItem) => current.byId.get(item.id) ?? `${item.type}-${item.id}`,
  };
}

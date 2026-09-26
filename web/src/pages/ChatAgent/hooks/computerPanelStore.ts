/**
 * Which computer surface is open, as a tiny external store.
 *
 * The Computers dialog is opened from places that share no parent below the
 * page root: the gallery header, a workspace card's machine line, and the disk
 * warnings in the gallery and in a chat. So the dialog mounts once at the root
 * and every opener writes here instead of threading callbacks down.
 */
import { useSyncExternalStore } from 'react';

import { registerAuthReset } from '@/lib/authResets';
import { createValueStore } from '@/lib/valueStore';

export interface ComputersPanelState {
  /** The Computers dialog: null when closed. `computerId` scrolls to and
   *  highlights that row; `expandStorage` opens its breakdown. */
  panel: { computerId: string | null; expandStorage: boolean } | null;
  /** The change-spec dialog for one machine, opened on its own. */
  specComputerId: string | null;
}

const INITIAL: ComputersPanelState = { panel: null, specComputerId: null };
const store = createValueStore<ComputersPanelState>(INITIAL);
// Survives the page unmount, so a sign-out must not reopen it for the next account.
registerAuthReset(() => store.set(INITIAL));

function set(next: Partial<ComputersPanelState>) {
  store.set({ ...store.get(), ...next });
}

export function openComputersPanel(opts: { computerId?: string | null; expandStorage?: boolean } = {}) {
  set({ panel: { computerId: opts.computerId ?? null, expandStorage: opts.expandStorage ?? false } });
}

export function closeComputersPanel() {
  set({ panel: null });
}

export function openComputerSpec(computerId: string) {
  set({ specComputerId: computerId });
}

export function closeComputerSpec() {
  set({ specComputerId: null });
}

export function useComputersPanel(): ComputersPanelState {
  return useSyncExternalStore(store.subscribe, store.get);
}

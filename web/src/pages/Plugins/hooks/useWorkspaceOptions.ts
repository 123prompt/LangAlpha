import { useTranslation } from 'react-i18next';
import { useWorkspaces } from '@/hooks/useWorkspaces';
import type { ScopeWorkspace } from '../components/ScopeControl';
import { useFlashWorkspace } from './useFlashWorkspace';

/**
 * The user's workspaces as the Plugins page consumes them: scope-control
 * options with a display name already resolved, plus the id → name lookup the
 * deck headers read.
 */

export interface WorkspaceOptions {
  /** Real workspaces only. Flash is never a member: a surface that folds it in
   * here offers it as a move target and as a bulk destination, neither of
   * which it can be. */
  workspaces: ScopeWorkspace[];
  /** Both tiers, so a deck header still resolves a Flash-scoped name. */
  nameById: Map<string, string>;
}

export function useWorkspaceOptions(): WorkspaceOptions {
  const { t } = useTranslation();
  const { data } = useWorkspaces({ limit: 100 });
  const flashWorkspace = useFlashWorkspace();

  const rows = data?.workspaces ?? [];
  const workspaces: ScopeWorkspace[] = rows.map((w) => ({
    id: w.workspace_id,
    name: w.name || t('plugins.scope.unknownWorkspace'),
  }));
  return {
    workspaces,
    nameById: new Map(
      [...workspaces, ...(flashWorkspace ? [flashWorkspace] : [])].map((w) => [w.id, w.name]),
    ),
  };
}

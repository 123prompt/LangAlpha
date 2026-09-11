import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { queryKeys } from '@/lib/queryKeys';
import { getFlashWorkspace } from '@/pages/ChatAgent/utils/api/workspaces';
import type { ScopeWorkspace } from '../components/ScopeControl';

/**
 * The Flash workspace as a scope-control option, or `undefined` until it
 * resolves.
 *
 * Flash runs on one per-user workspace that the gallery listing hides. It is
 * upserted on first use, so ensure it here: a server can be switched off for
 * Flash before the user ever opens a Flash chat.
 */
export function useFlashWorkspace(): ScopeWorkspace | undefined {
  const { t } = useTranslation();
  const { data } = useQuery({
    queryKey: queryKeys.workspaces.flash(),
    queryFn: getFlashWorkspace,
    staleTime: Infinity,
  });
  const id = data?.workspace_id;
  return id ? { id, name: t('plugins.scope.flash') } : undefined;
}

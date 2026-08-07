import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Eye, EyeOff, KeyRound, Pencil, Plus, Trash2 } from 'lucide-react';
import { Loader } from '@/components/ui/loader';
import {
  useUserVaultSecrets,
  useCreateUserVaultSecret,
  useUpdateUserVaultSecret,
  useDeleteUserVaultSecret,
} from '@/hooks/useUserVault';
import { formatApiErrorDetail, revealUserVaultSecret } from '@/pages/ChatAgent/utils/api';

/**
 * The Connectors → Secrets tab: user-level vault CRUD. These secrets back
 * `${vault:NAME}` refs on inherited (user-level) MCP servers and are merged
 * into every sandbox push — a same-named workspace secret wins, so a
 * workspace can always override a user default.
 */

export function ConnectorSecrets() {
  const { t } = useTranslation();
  const { data, isLoading, error: loadError } = useUserVaultSecrets();
  const createMutation = useCreateUserVaultSecret();
  const updateMutation = useUpdateUserVaultSecret();
  const deleteMutation = useDeleteUserVaultSecret();

  const [showAdd, setShowAdd] = useState(false);
  const [newName, setNewName] = useState('');
  const [newValue, setNewValue] = useState('');
  const [newDesc, setNewDesc] = useState('');
  const [showNewValue, setShowNewValue] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [editingName, setEditingName] = useState<string | null>(null);
  const [editValue, setEditValue] = useState('');
  const [editDesc, setEditDesc] = useState('');
  const [showEditValue, setShowEditValue] = useState(false);

  const [deletingName, setDeletingName] = useState<string | null>(null);
  const [revealingName, setRevealingName] = useState<string | null>(null);
  const [revealedSecrets, setRevealedSecrets] = useState<Record<string, string>>({});

  const secrets = data?.secrets ?? [];
  const maxSecrets = secrets.length + (data?.remaining_slots ?? 0);

  async function handleCreate() {
    setError(null);
    try {
      await createMutation.mutateAsync({
        name: newName,
        value: newValue,
        description: newDesc || undefined,
      });
      setShowAdd(false);
      setNewName('');
      setNewValue('');
      setNewDesc('');
      setShowNewValue(false);
    } catch (err) {
      setError(formatApiErrorDetail(err));
    }
  }

  async function handleUpdate(name: string) {
    setError(null);
    try {
      await updateMutation.mutateAsync({
        name,
        body: {
          ...(editValue ? { value: editValue } : {}),
          description: editDesc,
        },
      });
      setEditingName(null);
      setEditValue('');
      setEditDesc('');
      setShowEditValue(false);
      setRevealedSecrets((prev) => {
        const next = { ...prev };
        delete next[name];
        return next;
      });
    } catch (err) {
      setError(formatApiErrorDetail(err));
    }
  }

  async function handleDelete(name: string) {
    setError(null);
    try {
      await deleteMutation.mutateAsync(name);
      setDeletingName(null);
    } catch (err) {
      setError(formatApiErrorDetail(err));
    }
  }

  async function handleRevealToggle(name: string) {
    if (revealedSecrets[name] !== undefined) {
      setRevealedSecrets((prev) => {
        const next = { ...prev };
        delete next[name];
        return next;
      });
      return;
    }
    setRevealingName(name);
    try {
      const value = await revealUserVaultSecret(name);
      setRevealedSecrets((prev) => ({ ...prev, [name]: value }));
    } catch (err) {
      setError(formatApiErrorDetail(err));
    } finally {
      setRevealingName(null);
    }
  }

  if (isLoading) {
    return (
      <div className="flex flex-col gap-3">
        {[1, 2].map((i) => (
          <div key={i} className="h-14 rounded-lg animate-pulse" style={{ backgroundColor: 'var(--color-bg-card)' }} />
        ))}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <KeyRound className="h-4 w-4" style={{ color: 'var(--color-accent-primary)' }} />
          <span className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
            {t('connectors.secrets.title')}
          </span>
          <span className="text-xs px-1.5 py-0.5 rounded" style={{ color: 'var(--color-text-tertiary)', backgroundColor: 'var(--color-bg-card)' }}>
            {secrets.length} / {maxSecrets}
          </span>
        </div>
        {(data?.remaining_slots ?? 0) > 0 && (
          <button
            type="button"
            onClick={() => { setShowAdd(!showAdd); setError(null); }}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-md transition-colors"
            style={{ color: 'var(--color-btn-primary-text)', backgroundColor: 'var(--color-btn-primary-bg)' }}
          >
            <Plus className="h-3 w-3" />
            {t('connectors.secrets.addSecret')}
          </button>
        )}
      </div>

      <p className="text-[0.6875rem]" style={{ color: 'var(--color-text-tertiary)' }}>
        {t('connectors.secrets.scopeHint')}
      </p>

      {(error || loadError) && (
        <div className="text-xs p-2 rounded" style={{ backgroundColor: 'var(--color-bg-card)', color: 'var(--color-loss)' }}>
          {error || (loadError as { message?: string })?.message || t('connectors.secrets.loadFailed')}
        </div>
      )}

      {showAdd && (
        <div
          className="flex flex-col gap-2 p-3 rounded-lg"
          style={{ backgroundColor: 'var(--color-bg-card)', border: '1px solid var(--color-border-muted)' }}
        >
          <input
            type="text"
            value={newName}
            onChange={(e) => setNewName(e.target.value.toUpperCase().replace(/[^A-Z0-9_]/g, '').replace(/^[0-9]+/, ''))}
            placeholder="SECRET_NAME"
            className="w-full px-3 py-2 text-sm rounded-md bg-transparent outline-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-[color:var(--color-accent-primary)] font-mono"
            style={{ color: 'var(--color-text-primary)', border: '1px solid var(--color-border-muted)' }}
            maxLength={64}
          />
          <div className="relative">
            <input
              type={showNewValue ? 'text' : 'password'}
              value={newValue}
              onChange={(e) => setNewValue(e.target.value)}
              placeholder={t('connectors.secrets.valuePlaceholder')}
              className="w-full px-3 py-2 pr-9 text-sm rounded-md bg-transparent outline-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-[color:var(--color-accent-primary)]"
              style={{ color: 'var(--color-text-primary)', border: '1px solid var(--color-border-muted)' }}
              maxLength={4096}
            />
            <button
              type="button"
              onClick={() => setShowNewValue(!showNewValue)}
              className="absolute right-2 top-1/2 -translate-y-1/2 p-1 rounded transition-colors hover:bg-foreground/10"
              style={{ color: 'var(--color-text-tertiary)' }}
              tabIndex={-1}
            >
              {showNewValue ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
            </button>
          </div>
          <input
            type="text"
            value={newDesc}
            onChange={(e) => setNewDesc(e.target.value)}
            placeholder={t('connectors.secrets.descriptionPlaceholder')}
            className="w-full px-3 py-2 text-sm rounded-md bg-transparent outline-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-[color:var(--color-accent-primary)]"
            style={{ color: 'var(--color-text-primary)', border: '1px solid var(--color-border-muted)' }}
            maxLength={256}
          />
          <div className="flex justify-end gap-2 mt-1">
            <button
              type="button"
              onClick={() => { setShowAdd(false); setError(null); }}
              className="px-3 py-1.5 text-xs rounded-md transition-colors hover:bg-foreground/10"
              style={{ color: 'var(--color-text-tertiary)' }}
            >
              {t('common.cancel')}
            </button>
            <button
              type="button"
              onClick={handleCreate}
              disabled={createMutation.isPending || !newName || !newValue}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-md transition-colors disabled:opacity-50"
              style={{ color: 'var(--color-btn-primary-text)', backgroundColor: 'var(--color-btn-primary-bg)' }}
            >
              {createMutation.isPending && <Loader size={12} className="text-current" />}
              {t('common.save')}
            </button>
          </div>
        </div>
      )}

      {secrets.length === 0 && !showAdd ? (
        <div className="py-8 text-center text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
          {t('connectors.secrets.empty')}
        </div>
      ) : (
        <div className="flex flex-col gap-1">
          {secrets.map((secret) => (
            <div key={secret.user_vault_secret_id}>
              {editingName === secret.name ? (
                <div
                  className="flex flex-col gap-2 p-3 rounded-lg"
                  style={{ backgroundColor: 'var(--color-bg-card)', border: '1px solid var(--color-border-elevated)' }}
                >
                  <div className="text-sm font-mono font-medium" style={{ color: 'var(--color-text-primary)' }}>
                    {secret.name}
                  </div>
                  <div className="relative">
                    <input
                      type={showEditValue ? 'text' : 'password'}
                      value={editValue}
                      onChange={(e) => setEditValue(e.target.value)}
                      placeholder={t('connectors.secrets.editValuePlaceholder')}
                      className="w-full px-3 py-2 pr-9 text-sm rounded-md bg-transparent outline-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-[color:var(--color-accent-primary)]"
                      style={{ color: 'var(--color-text-primary)', border: '1px solid var(--color-border-muted)' }}
                      maxLength={4096}
                    />
                    <button
                      type="button"
                      onClick={() => setShowEditValue(!showEditValue)}
                      className="absolute right-2 top-1/2 -translate-y-1/2 p-1 rounded transition-colors hover:bg-foreground/10"
                      style={{ color: 'var(--color-text-tertiary)' }}
                      tabIndex={-1}
                    >
                      {showEditValue ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                    </button>
                  </div>
                  <input
                    type="text"
                    value={editDesc}
                    onChange={(e) => setEditDesc(e.target.value)}
                    placeholder={t('connectors.secrets.descriptionPlaceholder')}
                    className="w-full px-3 py-2 text-sm rounded-md bg-transparent outline-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-[color:var(--color-accent-primary)]"
                    style={{ color: 'var(--color-text-primary)', border: '1px solid var(--color-border-muted)' }}
                    maxLength={256}
                  />
                  <div className="flex justify-end gap-2 mt-1">
                    <button
                      type="button"
                      onClick={() => { setEditingName(null); setEditValue(''); setEditDesc(''); setShowEditValue(false); }}
                      className="px-3 py-1.5 text-xs rounded-md transition-colors hover:bg-foreground/10"
                      style={{ color: 'var(--color-text-tertiary)' }}
                    >
                      {t('common.cancel')}
                    </button>
                    <button
                      type="button"
                      onClick={() => handleUpdate(secret.name)}
                      disabled={updateMutation.isPending}
                      className="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-md transition-colors disabled:opacity-50"
                      style={{ color: 'var(--color-btn-primary-text)', backgroundColor: 'var(--color-btn-primary-bg)' }}
                    >
                      {updateMutation.isPending && <Loader size={12} className="text-current" />}
                      {t('connectors.secrets.update')}
                    </button>
                  </div>
                </div>
              ) : (
                <div
                  className="flex items-center justify-between py-2.5 px-3 rounded-lg"
                  style={{ backgroundColor: 'var(--color-bg-card)' }}
                >
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-mono font-medium" style={{ color: 'var(--color-text-primary)' }}>
                        {secret.name}
                      </span>
                      <span className="text-xs font-mono truncate" style={{ color: 'var(--color-text-tertiary)' }}>
                        {revealedSecrets[secret.name] !== undefined ? revealedSecrets[secret.name] : secret.masked_value}
                      </span>
                    </div>
                    {secret.description && (
                      <p className="text-xs mt-0.5 truncate" style={{ color: 'var(--color-text-tertiary)' }}>
                        {secret.description}
                      </p>
                    )}
                  </div>
                  <div className="flex items-center gap-1 flex-shrink-0 ml-2">
                    <button
                      type="button"
                      onClick={() => handleRevealToggle(secret.name)}
                      disabled={revealingName === secret.name}
                      className="p-1.5 rounded transition-colors hover:bg-foreground/10 disabled:opacity-50"
                      style={{ color: 'var(--color-text-tertiary)' }}
                      title={revealedSecrets[secret.name] !== undefined ? t('connectors.secrets.hideValue') : t('connectors.secrets.revealValue')}
                    >
                      {revealingName === secret.name ? (
                        <Loader size={14} className="text-current" />
                      ) : revealedSecrets[secret.name] !== undefined ? (
                        <EyeOff className="h-3.5 w-3.5" />
                      ) : (
                        <Eye className="h-3.5 w-3.5" />
                      )}
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setEditingName(secret.name);
                        setEditValue('');
                        setEditDesc(secret.description);
                        setError(null);
                      }}
                      className="p-1.5 rounded transition-colors hover:bg-foreground/10"
                      style={{ color: 'var(--color-text-tertiary)' }}
                      title={t('connectors.secrets.edit')}
                    >
                      <Pencil className="h-3.5 w-3.5" />
                    </button>
                    {deletingName === secret.name ? (
                      <div className="flex items-center gap-1">
                        <button
                          type="button"
                          onClick={() => handleDelete(secret.name)}
                          disabled={deleteMutation.isPending}
                          className="px-2 py-1 text-xs rounded transition-colors disabled:opacity-50"
                          style={{ color: 'var(--color-loss)', backgroundColor: 'var(--color-bg-card)' }}
                        >
                          {deleteMutation.isPending ? t('common.loading') : t('connectors.secrets.deleteConfirmYes')}
                        </button>
                        <button
                          type="button"
                          onClick={() => setDeletingName(null)}
                          className="px-2 py-1 text-xs rounded transition-colors hover:bg-foreground/10"
                          style={{ color: 'var(--color-text-tertiary)' }}
                        >
                          {t('common.cancel')}
                        </button>
                      </div>
                    ) : (
                      <button
                        type="button"
                        onClick={() => setDeletingName(secret.name)}
                        className="p-1.5 rounded transition-colors hover:bg-foreground/10"
                        style={{ color: 'var(--color-text-tertiary)' }}
                        title={t('connectors.secrets.delete')}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    )}
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

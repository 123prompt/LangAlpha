import React, { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ExternalLink, Eye, EyeOff, KeyRound, Pencil, Plus, Sparkles, Trash2 } from 'lucide-react';
import { Loader } from '@/components/ui/loader';
import { ListEmpty, ListError, ListHeader, ListSkeleton } from '../mcp/McpPrimitives';
import { formatApiErrorDetail, type VaultBlueprint } from '../../utils/api';

/**
 * The one vault-secrets manager, shared by the two scopes: the workspace Vault
 * tab and the Connectors → Secrets page. It owns the entire add/edit/reveal/
 * delete state machine; callers supply the data and the four async operations
 * (React Query mutations on the user side, plain API + reload on the workspace
 * side) plus the scope-specific extras — blueprints, prefill deep-link, hint
 * copy, footer.
 */

const NAME_RE = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;

export interface SecretItem {
  id: string;
  name: string;
  description: string;
  masked_value: string;
}

export interface SecretsManagerProps {
  title: string;
  secrets: SecretItem[];
  maxSecrets: number;
  loading: boolean;
  loadError?: string | null;
  /** Scope explainer rendered under the header. */
  hint?: React.ReactNode;
  emptyText: string;
  /** "Recommended credentials" cards (declared by enabled MCP servers). */
  blueprints?: VaultBlueprint[];
  /** Deep-link (e.g. an MCP "Set up NAME" affordance): opens the add form prefilled. */
  prefillSecretName?: string | null;
  onPrefillConsumed?: () => void;
  onCreate: (body: { name: string; value: string; description?: string }) => Promise<unknown>;
  onUpdate: (name: string, body: { value?: string; description?: string }) => Promise<unknown>;
  onDelete: (name: string) => Promise<unknown>;
  onReveal: (name: string) => Promise<string>;
  /** Scope-specific trailing content (e.g. the workspace usage/security card). */
  footer?: React.ReactNode;
}

export function SecretsManager({
  title,
  secrets,
  maxSecrets,
  loading,
  loadError,
  hint,
  emptyText,
  blueprints = [],
  prefillSecretName,
  onPrefillConsumed,
  onCreate,
  onUpdate,
  onDelete,
  onReveal,
  footer,
}: SecretsManagerProps) {
  const { t } = useTranslation();

  // Add form
  const [showAdd, setShowAdd] = useState(false);
  const [newName, setNewName] = useState('');
  const [newValue, setNewValue] = useState('');
  const [newDesc, setNewDesc] = useState('');
  const [showNewValue, setShowNewValue] = useState(false);
  const [saving, setSaving] = useState(false);
  // When opened via a blueprint "Set up" click, carry the docs link + regex
  // for inline hint rendering. Cleared when the form closes or save succeeds.
  const [presetBlueprint, setPresetBlueprint] = useState<VaultBlueprint | null>(null);

  // Edit state
  const [editingName, setEditingName] = useState<string | null>(null);
  const [editValue, setEditValue] = useState('');
  const [editDesc, setEditDesc] = useState('');
  const [showEditValue, setShowEditValue] = useState(false);
  const [editSaving, setEditSaving] = useState(false);

  // Delete / reveal
  const [deletingName, setDeletingName] = useState<string | null>(null);
  const [deletePending, setDeletePending] = useState(false);
  const [revealingName, setRevealingName] = useState<string | null>(null);
  const [revealedSecrets, setRevealedSecrets] = useState<Record<string, string>>({});

  const [error, setError] = useState<string | null>(null);

  function closeAddForm() {
    setShowAdd(false);
    setNewName('');
    setNewValue('');
    setNewDesc('');
    setShowNewValue(false);
    setPresetBlueprint(null);
  }

  useEffect(() => {
    if (!prefillSecretName) return;
    setError(null);
    setPresetBlueprint(null);
    setNewName(prefillSecretName);
    setNewValue('');
    setNewDesc('');
    setShowNewValue(false);
    setShowAdd(true);
    onPrefillConsumed?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prefillSecretName]);

  // Safe regex compile for the active preset blueprint. Invalid patterns from a
  // misconfigured agent_config.yaml must not crash the UI — on failure we just
  // skip the hint.
  const presetRegex = useMemo<RegExp | null>(() => {
    if (!presetBlueprint?.regex) return null;
    try {
      return new RegExp(presetBlueprint.regex);
    } catch {
      return null;
    }
  }, [presetBlueprint]);
  const valueHintFailing =
    presetRegex !== null && newValue.length > 0 && !presetRegex.test(newValue);

  function openAddForBlueprint(bp: VaultBlueprint) {
    setError(null);
    setPresetBlueprint(bp);
    setNewName(bp.name);
    setNewValue('');
    setNewDesc(bp.description || '');
    setShowNewValue(false);
    setShowAdd(true);
  }

  async function handleCreate() {
    if (!newName || !newValue) return;
    if (!NAME_RE.test(newName)) {
      setError(t('vault.nameInvalid'));
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await onCreate({ name: newName, value: newValue, description: newDesc || undefined });
      closeAddForm();
    } catch (err) {
      setError(formatApiErrorDetail(err));
    } finally {
      setSaving(false);
    }
  }

  async function handleUpdate(name: string) {
    setEditSaving(true);
    setError(null);
    try {
      await onUpdate(name, { ...(editValue ? { value: editValue } : {}), description: editDesc });
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
    } finally {
      setEditSaving(false);
    }
  }

  async function handleDelete(name: string) {
    setDeletePending(true);
    setError(null);
    try {
      await onDelete(name);
      setDeletingName(null);
    } catch (err) {
      setError(formatApiErrorDetail(err));
    } finally {
      setDeletePending(false);
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
    setError(null);
    try {
      const value = await onReveal(name);
      setRevealedSecrets((prev) => ({ ...prev, [name]: value }));
    } catch (err) {
      setError(formatApiErrorDetail(err));
    } finally {
      setRevealingName(null);
    }
  }

  const inputClass =
    'w-full px-3 py-2 text-sm rounded-md bg-transparent outline-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-[color:var(--color-accent-primary)]';
  const inputStyle: React.CSSProperties = {
    color: 'var(--color-text-primary)',
    border: '1px solid var(--color-border-muted)',
  };

  if (loading) {
    return <ListSkeleton rows={2} />;
  }

  return (
    <div className="flex flex-col gap-4">
      <ListHeader icon={KeyRound} title={title} count={secrets.length} max={maxSecrets}>
        {secrets.length < maxSecrets && (
          <button
            type="button"
            onClick={() => { setShowAdd(!showAdd); setError(null); setPresetBlueprint(null); }}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-md transition-colors"
            style={{ color: 'var(--color-btn-primary-text)', backgroundColor: 'var(--color-btn-primary-bg)' }}
          >
            <Plus className="h-3 w-3" />
            {t('vault.addSecret')}
          </button>
        )}
      </ListHeader>

      {hint && (
        <p className="text-[0.6875rem]" style={{ color: 'var(--color-text-tertiary)' }}>
          {hint}
        </p>
      )}

      {(error || loadError) && <ListError>{error || loadError}</ListError>}

      {/* Recommended credentials — blueprints declared by enabled MCP servers */}
      {blueprints.length > 0 && !showAdd && (
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-1.5 text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
            <Sparkles className="h-3 w-3" />
            {t('vault.recommended')}
          </div>
          {blueprints.map((bp) => {
            const disabled = secrets.length >= maxSecrets;
            return (
              <button
                key={bp.name}
                type="button"
                onClick={() => openAddForBlueprint(bp)}
                disabled={disabled}
                className="flex flex-col items-start gap-0.5 p-3 rounded-lg text-left transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                style={{
                  backgroundColor: 'var(--color-bg-card)',
                  border: '1px dashed var(--color-border-default)',
                }}
                title={disabled ? t('vault.atCapHint', { max: maxSecrets }) : undefined}
              >
                <div className="flex items-center justify-between w-full gap-2">
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="text-sm font-medium truncate" style={{ color: 'var(--color-text-primary)' }}>
                      {bp.label}
                    </span>
                    <span className="text-xs font-mono px-1.5 py-0.5 rounded flex-shrink-0" style={{ color: 'var(--color-text-tertiary)', backgroundColor: 'var(--color-bg-tag)' }}>
                      {bp.name}
                    </span>
                  </div>
                  <span className="text-xs flex items-center gap-1 flex-shrink-0" style={{ color: 'var(--color-accent-primary)' }}>
                    <Plus className="h-3 w-3" />
                    {t('vault.setUp')}
                  </span>
                </div>
                {bp.description && (
                  <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                    {bp.description}
                  </div>
                )}
              </button>
            );
          })}
        </div>
      )}

      {/* Add form */}
      {showAdd && (
        <div
          className="flex flex-col gap-2 p-3 rounded-lg"
          style={{ backgroundColor: 'var(--color-bg-card)', border: '1px solid var(--color-border-muted)' }}
        >
          {presetBlueprint && (
            <div className="flex items-center justify-between text-xs" style={{ color: 'var(--color-text-secondary)' }}>
              <span>
                {t('vault.settingUp')}{' '}
                <span style={{ color: 'var(--color-text-primary)' }}>{presetBlueprint.label}</span>
              </span>
              {presetBlueprint.docs_url && (
                <a
                  href={presetBlueprint.docs_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 hover:underline"
                  style={{ color: 'var(--color-accent-primary)' }}
                >
                  {t('vault.docs')} <ExternalLink className="h-3 w-3" />
                </a>
              )}
            </div>
          )}
          <input
            type="text"
            value={newName}
            onChange={(e) => setNewName(e.target.value.toUpperCase().replace(/[^A-Z0-9_]/g, '').replace(/^[0-9]+/, ''))}
            placeholder="SECRET_NAME"
            className={`${inputClass} font-mono`}
            style={inputStyle}
            maxLength={64}
          />
          <div className="relative">
            <input
              type={showNewValue ? 'text' : 'password'}
              value={newValue}
              onChange={(e) => setNewValue(e.target.value)}
              placeholder={t('vault.valuePlaceholder')}
              className={`${inputClass} pr-9`}
              style={inputStyle}
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
          {valueHintFailing && (
            <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              {t('vault.valueHintInvalid', { label: presetBlueprint?.label ?? t('vault.tokenFallback') })}
              {presetBlueprint?.docs_url ? t('vault.valueHintDocs') : '.'}
              <span className="ml-1 opacity-70">{t('vault.valueHintStillSave')}</span>
            </div>
          )}
          <input
            type="text"
            value={newDesc}
            onChange={(e) => setNewDesc(e.target.value)}
            placeholder={t('vault.descriptionPlaceholder')}
            className={inputClass}
            style={inputStyle}
            maxLength={256}
          />
          <div className="flex justify-end gap-2 mt-1">
            <button
              type="button"
              onClick={() => { closeAddForm(); setError(null); }}
              className="px-3 py-1.5 text-xs rounded-md transition-colors hover:bg-foreground/10"
              style={{ color: 'var(--color-text-tertiary)' }}
            >
              {t('common.cancel')}
            </button>
            <button
              type="button"
              onClick={handleCreate}
              disabled={saving || !newName || !newValue}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-md transition-colors disabled:opacity-50"
              style={{ color: 'var(--color-btn-primary-text)', backgroundColor: 'var(--color-btn-primary-bg)' }}
            >
              {saving && <Loader size={12} className="text-current" />}
              {t('common.save')}
            </button>
          </div>
        </div>
      )}

      {/* Secret list */}
      {secrets.length === 0 && !showAdd ? (
        <ListEmpty>{emptyText}</ListEmpty>
      ) : (
        <div className="flex flex-col gap-1">
          {secrets.map((secret) => (
            <div key={secret.id}>
              {editingName === secret.name ? (
                /* Edit form */
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
                      placeholder={t('vault.editValuePlaceholder')}
                      className={`${inputClass} pr-9`}
                      style={inputStyle}
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
                    placeholder={t('vault.descriptionPlaceholder')}
                    className={inputClass}
                    style={inputStyle}
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
                      disabled={editSaving}
                      className="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-md transition-colors disabled:opacity-50"
                      style={{ color: 'var(--color-btn-primary-text)', backgroundColor: 'var(--color-btn-primary-bg)' }}
                    >
                      {editSaving && <Loader size={12} className="text-current" />}
                      {t('vault.update')}
                    </button>
                  </div>
                </div>
              ) : (
                /* Display row */
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
                      title={revealedSecrets[secret.name] !== undefined ? t('vault.hideValue') : t('vault.revealValue')}
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
                      title={t('vault.edit')}
                    >
                      <Pencil className="h-3.5 w-3.5" />
                    </button>
                    {deletingName === secret.name ? (
                      <div className="flex items-center gap-1">
                        <button
                          type="button"
                          onClick={() => handleDelete(secret.name)}
                          disabled={deletePending}
                          className="px-2 py-1 text-xs rounded transition-colors disabled:opacity-50"
                          style={{ color: 'var(--color-loss)', backgroundColor: 'var(--color-bg-card)' }}
                        >
                          {deletePending ? t('vault.deleting') : t('vault.deleteConfirmYes')}
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
                        title={t('vault.delete')}
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

      {footer}
    </div>
  );
}

import { useTranslation } from 'react-i18next';
import { EnabledToggle } from '@/pages/ChatAgent/components/mcp/McpPrimitives';
import { Select } from '@/components/ui/select';
import type {
  CatalogServer,
  McpServerBindingPatch,
  McpToolBinding,
  McpToolSummary,
} from '@/pages/ChatAgent/utils/api';

/**
 * How a server's tools reach the model, and the row-wide switch a broker
 * gets on top of that. Precedence is the whole reason this is two surfaces:
 * a tool's own override beats the row preset, which beats the group default,
 * so the control that sets the override sits on the tool and the preset up
 * here says what the rest fall back to.
 */

const BINDINGS: McpToolBinding[] = ['ptc', 'direct', 'both'];

/**
 * The row-wide switch. `binding_preset` has one non-null value, `ptc_only`,
 * which sends everything the row is allowed to move through the sandbox;
 * null leaves each group's own default in force, and for a group that
 * supports direct calls that default is direct, so anything other than
 * `ptc_only` reads as the switch being on. A tool the server pins, which for
 * a live order tool is to direct, stays where it is under either setting, so
 * the switch names what it reaches rather than promising to move everything.
 */
export function ToolAccessSwitches({
  catalog,
  busy,
  onPatch,
}: {
  catalog: CatalogServer;
  busy: boolean;
  onPatch: (body: McpServerBindingPatch) => void;
}) {
  const { t } = useTranslation();
  const groupDefaults = catalog.binding_preset !== 'ptc_only';
  return (
    <ul className="flex flex-col gap-3">
      <SwitchRow
        label={t('plugins.detail.groupDefaults')}
        desc={t('plugins.detail.groupDefaultsDesc')}
        enabled={groupDefaults}
        disabled={busy}
        onToggle={() => onPatch({ binding_preset: groupDefaults ? 'ptc_only' : null })}
      />
    </ul>
  );
}

function SwitchRow({
  label,
  desc,
  enabled,
  disabled,
  onToggle,
}: {
  label: string;
  desc: string;
  enabled: boolean;
  disabled: boolean;
  onToggle: () => void;
}) {
  return (
    <li className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <p className="text-xs font-medium" style={{ color: 'var(--color-text-primary)' }}>
          {label}
        </p>
        <p className="text-[0.6875rem] mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>
          {desc}
        </p>
      </div>
      <div className="flex-shrink-0 pt-0.5">
        <EnabledToggle enabled={enabled} name={label} disabled={disabled} onToggle={onToggle} />
      </div>
    </li>
  );
}

/**
 * One tool's binding, as the compact control at the right edge of its row.
 * Writes the override every time: the server owns the precedence, so the
 * client does not guess what the row would fall back to. Reset is the one
 * way back, and it removes the key rather than writing a matching value.
 */
export function ToolBindingControl({
  tool,
  catalog,
  busy,
  error,
  onPatch,
}: {
  tool: McpToolSummary;
  catalog: CatalogServer;
  busy: boolean;
  /** The server's refusal of the last change to this tool, verbatim. */
  error?: string | null;
  onPatch: (body: McpServerBindingPatch) => void;
}) {
  const { t } = useTranslation();
  const overridden = tool.binding_source === 'override';
  // A pinned tool (a live order tool, which the server holds to direct) gets
  // a 422 for any other value. Say so on the row rather than let the user
  // find out by picking one. Which values it takes comes from `allowed`, not
  // from the pin: an absent list is an unrestricted tool, never a locked one.
  const pinned = tool.binding_source === 'policy';
  const allowed = tool.allowed;
  const permitted = (b: McpToolBinding) => allowed == null || allowed.includes(b);
  const value = tool.binding ?? 'ptc';

  // Only this tool travels. A stored key that is another spelling of the same
  // name is dropped server-side, which reads the map folded anyway, so the
  // page never has to carry the map to write one switch.
  function write(next: McpToolBinding) {
    onPatch({ tool_binding_set: { [tool.name]: next } });
  }
  function reset() {
    onPatch({ tool_binding_unset: [tool.name] });
  }

  return (
    <div className="flex flex-col items-end gap-0.5 flex-shrink-0">
      <div className="flex items-center gap-1.5">
        {pinned && (
          <span
            className="text-[0.625rem] text-right"
            style={{ color: 'var(--color-text-quaternary)' }}
          >
            {t('plugins.detail.bindingPinned')}
          </span>
        )}
        {overridden && (
          <>
            <span className="text-[0.625rem]" style={{ color: 'var(--color-text-quaternary)' }}>
              {t('plugins.detail.bindingCustom')}
            </span>
            <button
              type="button"
              disabled={busy}
              onClick={reset}
              aria-label={t('plugins.detail.bindingResetAria', { name: tool.name })}
              className="text-[0.625rem] hover:underline underline-offset-2 disabled:opacity-50"
              style={{ color: 'var(--color-text-tertiary)' }}
            >
              {t('plugins.detail.bindingReset')}
            </button>
          </>
        )}
        <Select
          value={value}
          disabled={busy}
          aria-label={t('plugins.detail.bindingAria', { name: tool.name })}
          onChange={(e) => write(e.target.value as McpToolBinding)}
          className="w-[6.5rem]"
          style={{
            height: '1.5rem',
            fontSize: '0.6875rem',
            paddingLeft: '0.5rem',
            paddingRight: '1.75rem',
          }}
        >
          {BINDINGS.map((b) => (
            <option key={b} value={b} disabled={!permitted(b)}>
              {t(`plugins.detail.binding_${b}`)}
            </option>
          ))}
        </Select>
      </div>
      {error && (
        <span role="alert" className="text-[0.625rem] text-right" style={{ color: 'var(--color-loss)' }}>
          {error}
        </span>
      )}
    </div>
  );
}

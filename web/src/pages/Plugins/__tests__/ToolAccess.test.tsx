import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import { renderWithProviders } from '@/test/utils';
import { httpCatalogServer } from '@/test/factories';
import type { CatalogServer, McpToolSummary } from '@/pages/ChatAgent/utils/api';
import type { Brokerage } from '../brokerages';

/**
 * What the detail panel writes when someone changes how the agent reaches a
 * broker's tools. Every assertion is on the patch body, because precedence
 * lives on the server: the row preset, the per-tool override, and the reset
 * that removes it are three different writes and the panel must send the one
 * the control it drew stands for.
 */

const VENDOR: Brokerage = {
  name: 'moomoo',
  label: 'moomoo',
  url: 'https://mcp.example.com/moomoo',
  site: 'moomoo.com',
  description: '',
  native_callback_only: false,
  exclusive_connection: false,
  capabilities: [
    { key: 'account', tone: 'caution' },
    { key: 'paper_trading', tone: 'caution', rung: true },
    { key: 'trading', tone: 'danger', rung: true },
  ],
};

const TOOLS: McpToolSummary[] = [
  {
    name: 'existing_tool',
    description: '',
    input_schema: {},
    capability: 'account',
    binding: 'direct',
    binding_source: 'override',
    allowed: ['ptc', 'direct', 'both'],
  },
  {
    name: 'sim_trade_account_list',
    description: '',
    input_schema: {},
    capability: 'paper_trading',
    binding: 'direct',
    binding_source: 'group',
  },
  {
    name: 'trading_order_place',
    description: '',
    input_schema: {},
    capability: 'trading',
    binding: 'direct',
    binding_source: 'policy',
    allowed: ['direct'],
  },
];

const patch = vi.fn().mockResolvedValue({});
vi.mock('@/hooks/useMcpServers', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/hooks/useMcpServers')>();
  return {
    ...actual,
    useSetMcpServerBinding: () => ({ mutateAsync: patch, isPending: false }),
    useMcpCatalogServerTools: () => ({
      data: { tools: TOOLS, discovered_at: null },
      isLoading: false,
      isError: false,
    }),
    useBuiltinMcpServerTools: () => ({ data: undefined, isLoading: false, isError: false }),
    useBrokerages: () => ({ data: [VENDOR], isLoading: false, error: null }),
  };
});

function row(over: Partial<CatalogServer> = {}): CatalogServer {
  return httpCatalogServer({
    name: 'moomoo',
    url: VENDOR.url,
    oauth_status: 'connected',
    granted_capabilities: ['account', 'paper_trading', 'trading'],
    tool_binding: { existing_tool: 'direct' },
    binding_preset: null,
    ...over,
  });
}

async function renderDetail(over: Partial<CatalogServer> = {}) {
  const { ServerDetail } = await import('../components/ServerDetail');
  return renderWithProviders(
    <ServerDetail
      data={{ origin: 'brokerage', brokerage: VENDOR, server: row(over) }}
      onClose={() => {}}
    />,
  );
}

const bindingSelect = (tool: string) =>
  screen.getByRole('combobox', { name: `How the agent reaches ${tool}` });

beforeEach(() => {
  patch.mockClear();
  patch.mockResolvedValue({});
});

describe('the tool access section', () => {
  it('reads an unset preset as the direct switch already being on', async () => {
    await renderDetail();
    const toggle = screen.getByRole('switch', { name: /Use each tool group's default/ });

    expect(toggle).toHaveAttribute('aria-checked', 'true');
    fireEvent.click(toggle);
    await waitFor(() =>
      expect(patch).toHaveBeenCalledWith({
        name: 'moomoo',
        body: { binding_preset: 'ptc_only' },
      }),
    );
  });

  it('clears the preset to null when switched back on', async () => {
    await renderDetail({ binding_preset: 'ptc_only' });
    const toggle = screen.getByRole('switch', { name: /Use each tool group's default/ });

    expect(toggle).toHaveAttribute('aria-checked', 'false');
    fireEvent.click(toggle);
    await waitFor(() =>
      expect(patch).toHaveBeenCalledWith({
        name: 'moomoo',
        body: { binding_preset: null },
      }),
    );
  });

  // A row written before the preset lost its second value may still hold the
  // old string; anything other than `ptc_only` is the on position.
  it('reads a legacy stored preset as the switch being on', async () => {
    await renderDetail({ binding_preset: 'order_direct' as unknown as CatalogServer['binding_preset'] });
    const toggle = screen.getByRole('switch', { name: /Use each tool group's default/ });

    expect(toggle).toHaveAttribute('aria-checked', 'true');
  });

  // The override is written whole: the row's existing overrides travel with it,
  // so setting one tool never silently drops another's.
  // Only the tool the user touched travels. Carrying the rest of the map is
  // what let a second tab's edit be written back to the version this one read.
  it('writes only the tool it changed, never the stored map', async () => {
    await renderDetail();
    fireEvent.change(bindingSelect('sim_trade_account_list'), { target: { value: 'both' } });

    await waitFor(() =>
      expect(patch).toHaveBeenCalledWith({
        name: 'moomoo',
        body: { tool_binding_set: { sim_trade_account_list: 'both' } },
      }),
    );
  });

  it('offers only Direct on a live order tool the server pins to direct', async () => {
    // The server answers `ptc` or `both` on a live order tool with a 422, so
    // the row says where the tool runs rather than let the user find out by
    // picking a value that comes straight back.
    await renderDetail();
    const select = bindingSelect('trading_order_place');

    expect(within(select).getByRole('option', { name: 'PTC' })).toBeDisabled();
    expect(within(select).getByRole('option', { name: 'Both' })).toBeDisabled();
    expect(within(select).getByRole('option', { name: 'Direct' })).not.toBeDisabled();
    expect(
      screen.getByText('Live order tools run only as direct calls the app can show.'),
    ).toBeInTheDocument();
  });

  // The enable rule is `allowed`, not the pin: an option outside the list is
  // disabled and one inside it is not, whichever direction the server chose.
  it('disables exactly the options outside allowed', async () => {
    await renderDetail();
    const select = bindingSelect('existing_tool');

    for (const name of ['PTC', 'Direct', 'Both']) {
      expect(within(select).getByRole('option', { name })).not.toBeDisabled();
    }
    const pinned = bindingSelect('trading_order_place');
    expect(within(pinned).getByRole('option', { name: 'Direct' })).not.toBeDisabled();
    expect(within(pinned).getByRole('option', { name: 'PTC' })).toBeDisabled();
  });

  // A response without `allowed` is a stale one, not a locked tool: every
  // option stays live so an older server never freezes the whole control.
  it('treats a missing allowed list as unrestricted', async () => {
    await renderDetail();
    const select = bindingSelect('sim_trade_account_list');

    for (const name of ['PTC', 'Direct', 'Both']) {
      expect(within(select).getByRole('option', { name })).not.toBeDisabled();
    }
  });

  it('resets an overridden tool by naming it, not by writing the default', async () => {
    await renderDetail();
    fireEvent.click(
      screen.getByRole('button', { name: 'Reset existing_tool to the row default' }),
    );

    await waitFor(() =>
      expect(patch).toHaveBeenCalledWith({
        name: 'moomoo',
        body: { tool_binding_unset: ['existing_tool'] },
      }),
    );
  });

  // A stored key may be a differently cased or padded spelling of the
  // discovered name. Reconciling that is the server's job now: it reads the
  // map folded, and the delta names the tool rather than carrying the map, so
  // the page has no spellings to reconcile. Covered by ``merge_overrides``.

  it('shows the server refusal in the words the server sent', async () => {
    patch.mockRejectedValueOnce({
      response: {
        data: {
          detail:
            "'trading_order_place' places live orders, so it runs only as a direct call and cannot be bound from the sandbox",
        },
      },
    });
    await renderDetail();
    fireEvent.change(bindingSelect('sim_trade_account_list'), { target: { value: 'both' } });

    expect(
      await screen.findByText(
        "'trading_order_place' places live orders, so it runs only as a direct call and cannot be bound from the sandbox",
      ),
    ).toBeInTheDocument();
  });
});

/**
 * A composer pick writes the account preference and raises a toast with an
 * undo. These pin the ways that write could leave the composer, the preference
 * and the undo disagreeing: a rollback to unset, a pick made before the
 * preference is known, an older write's toast or failure outliving a newer
 * pick, an undo pressed after the mode or the preference changed, a pick
 * made before the model list says what is reachable, and a pill with nothing
 * saved to show, including the device-local tuning that default still holds,
 * a pick of the model it already shows, and a send before the preference is
 * known.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, act, fireEvent, screen } from '@testing-library/react';
import { createRef, type Ref } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import ChatInput, { type ChatInputHandle } from '../chat-input';
import { ChatInputRegistry, ContextBus } from '@/lib/contextBus';
import { queryKeys } from '@/lib/queryKeys';

vi.mock('@/pages/ChatAgent/utils/api', () => ({
  getSkills: vi.fn().mockResolvedValue([]),
  getModelMetadata: vi.fn().mockResolvedValue({}),
}));

const mocks = vi.hoisted(() => ({
  preferences: null as unknown,
  modelsLoading: false,
  systemDefaults: { default_model: 'model-default', flash_model: 'model-flash-default' },
  mutateAsync: vi.fn(),
  mutate: vi.fn(),
  toast: vi.fn(),
}));

vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({ preferences: mocks.preferences, isLoading: false }),
}));

vi.mock('@/hooks/useAllModels', () => ({
  useAllModels: () => ({
    validModelNames: new Set(),
    metadata: { 'model-default': { reasoning_efforts: ['low', 'high'], reasoning_effort_default: 'high' } },
    isLoading: mocks.modelsLoading,
    systemDefaults: mocks.systemDefaults,
  }),
}));

vi.mock('@/hooks/useUpdatePreferences', () => ({
  useUpdatePreferences: () => ({ mutateAsync: mocks.mutateAsync, mutate: mocks.mutate }),
}));

vi.mock('../use-toast', () => ({
  useToast: () => ({ toast: mocks.toast }),
  toast: mocks.toast,
}));

// The menu's own rendering is not under test; two buttons reach the same
// onSelectModel the dropdown items call.
vi.mock('../chat-input.modelMenu', () => ({
  ChatInputModelMenu: ({ onSelectModel, disabled, selectedModel, reasoningEfforts }: {
    onSelectModel: (m: string) => void;
    disabled?: boolean;
    selectedModel: string | null;
    reasoningEfforts: string[];
  }) => (
    <>
      <span>{disabled ? 'menu-disabled' : 'menu-enabled'}</span>
      <span>{`pill:${selectedModel}`}</span>
      <span>{`efforts:${reasoningEfforts.join(',')}`}</span>
      <button type="button" onClick={() => onSelectModel('model-beta')}>pick-beta</button>
      <button type="button" onClick={() => onSelectModel('model-gamma')}>pick-gamma</button>
      <button type="button" onClick={() => onSelectModel('model-default')}>pick-default</button>
    </>
  ),
  ModelTriggerMeasure: () => null,
}));

/** A write that lands in the cache, as `useUpdatePreferences` does. */
function savePreference(patch: { model_preference: Record<string, string | null> }) {
  const prev = queryClient.getQueryData<{ model_preference?: object }>(queryKeys.user.preferences());
  queryClient.setQueryData(queryKeys.user.preferences(), {
    ...prev,
    model_preference: { ...prev?.model_preference, ...patch.model_preference },
  });
  return Promise.resolve();
}

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((r) => { resolve = r; });
  return { promise, resolve };
}

let queryClient: QueryClient;

// The undo reads the preference back from the query cache, where the real
// write lands, so the cache starts from the mocked preference. Seeded once:
// a rerender must not wipe what a write stored.
function tree(
  onModelChange: (m: string | null) => void,
  mode?: 'fast' | 'ptc',
  ref?: Ref<ChatInputHandle>,
) {
  if (!queryClient.getQueryData(queryKeys.user.preferences())) {
    queryClient.setQueryData(queryKeys.user.preferences(), mocks.preferences);
  }
  return (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ChatInput ref={ref} onSend={vi.fn()} onModelChange={onModelChange} mode={mode} />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe('ChatInput — a model pick and the preference it writes', () => {
  beforeEach(() => {
    ContextBus.__resetForTests();
    ChatInputRegistry.__resetForTests();
    Element.prototype.scrollIntoView = vi.fn();
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    mocks.preferences = { model_preference: { preferred_model: 'model-alpha' } };
    mocks.modelsLoading = false;
    mocks.systemDefaults = { default_model: 'model-default', flash_model: 'model-flash-default' };
    mocks.mutateAsync.mockReset();
    mocks.mutate.mockReset();
    localStorage.clear();
    mocks.toast.mockReset();
    mocks.toast.mockImplementation(() => ({ id: 't', dismiss: vi.fn(), update: vi.fn() }));
  });
  afterEach(() => {
    ContextBus.__resetForTests();
    ChatInputRegistry.__resetForTests();
  });

  it('follows the preference back to unset, as a rolled-back write leaves it', () => {
    const onModelChange = vi.fn();
    const { rerender } = render(tree(onModelChange));
    expect(onModelChange).toHaveBeenLastCalledWith('model-alpha');

    mocks.preferences = { model_preference: {} };
    rerender(tree(onModelChange));

    expect(onModelChange).toHaveBeenLastCalledWith(null);
  });

  it('keeps a pick local while the preference is unknown, so no undo can delete it', () => {
    mocks.preferences = null;
    const onModelChange = vi.fn();
    render(tree(onModelChange));

    fireEvent.click(screen.getByText('pick-beta'));

    expect(onModelChange).toHaveBeenLastCalledWith('model-beta');
    expect(mocks.mutateAsync).not.toHaveBeenCalled();
  });

  it('raises a toast only for the latest pick when writes overlap', async () => {
    const first = deferred();
    const second = deferred();
    mocks.mutateAsync.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    render(tree(vi.fn()));

    fireEvent.click(screen.getByText('pick-beta'));
    fireEvent.click(screen.getByText('pick-gamma'));
    await act(async () => { first.resolve(); await first.promise; });
    expect(mocks.toast).not.toHaveBeenCalled();

    await act(async () => { second.resolve(); await second.promise; });
    expect(mocks.toast).toHaveBeenCalledTimes(1);
  });

  it('stays quiet when a write a newer pick overtook fails', async () => {
    const first = deferred();
    mocks.mutateAsync
      .mockReturnValueOnce(first.promise.then(() => { throw new Error('offline'); }))
      .mockResolvedValueOnce(undefined);
    render(tree(vi.fn()));

    fireEvent.click(screen.getByText('pick-beta'));
    await act(async () => { fireEvent.click(screen.getByText('pick-gamma')); });
    await act(async () => { first.resolve(); await first.promise.catch(() => {}); });

    expect(mocks.toast).toHaveBeenCalledTimes(1);
    expect(mocks.toast.mock.calls[0][0].variant).toBeUndefined();
  });

  it('names the deployment default for the mode when nothing is saved, and sends none', () => {
    mocks.preferences = { model_preference: {} };
    const onModelChange = vi.fn();
    const { rerender } = render(tree(onModelChange, 'ptc'));
    expect(screen.getByText('pill:model-default')).toBeInTheDocument();
    // The controls are the named model's, so its ladder is offered.
    expect(screen.getByText('efforts:low,high')).toBeInTheDocument();
    expect(onModelChange).toHaveBeenLastCalledWith(null);

    rerender(tree(onModelChange, 'fast'));
    expect(screen.getByText('pill:model-flash-default')).toBeInTheDocument();
    expect(onModelChange).toHaveBeenLastCalledWith(null);
  });

  it('names the primary default on a flash composer when the deployment names no flash model', () => {
    mocks.preferences = { model_preference: {} };
    mocks.systemDefaults = { default_model: 'model-default', flash_model: '' };
    render(tree(vi.fn(), 'fast'));
    expect(screen.getByText('pill:model-default')).toBeInTheDocument();
  });

  it('writes nothing when the pick is the default the pill already names', async () => {
    mocks.preferences = { model_preference: {} };
    const onModelChange = vi.fn();
    render(tree(onModelChange, 'ptc'));

    await act(async () => { fireEvent.click(screen.getByText('pick-default')); });
    expect(mocks.mutateAsync).not.toHaveBeenCalled();
    expect(mocks.toast).not.toHaveBeenCalled();
    expect(onModelChange).toHaveBeenLastCalledWith(null);
  });

  it('sends no default-model tuning while the preference is unknown', () => {
    mocks.preferences = null;
    const ref = createRef<ChatInputHandle>();
    const { rerender } = render(tree(vi.fn(), 'ptc', ref));
    expect(ref.current?.getModelOptions()).toMatchObject({ model: null, reasoningEffort: null });

    // Known and unset: the default is what runs, so its tuning is what to send.
    mocks.preferences = { model_preference: {} };
    queryClient.setQueryData(queryKeys.user.preferences(), mocks.preferences);
    rerender(tree(vi.fn(), 'ptc', ref));
    expect(ref.current?.getModelOptions()).toMatchObject({ model: null, reasoningEffort: 'high' });
  });

  it('lifts device-local tuning for the default when nothing is saved', () => {
    mocks.preferences = { model_preference: {} };
    localStorage.setItem('reasoning_effort:model-default', 'low');
    render(tree(vi.fn(), 'ptc'));
    expect(mocks.mutate).toHaveBeenCalledWith(
      { model_preference: { profiles: { 'model-default': { reasoning_effort: 'low' } } } },
      expect.anything(),
    );
  });

  it('holds the model menu shut until the model list has loaded', () => {
    mocks.modelsLoading = true;
    const { rerender } = render(tree(vi.fn()));
    expect(screen.getByText('menu-disabled')).toBeInTheDocument();

    mocks.modelsLoading = false;
    rerender(tree(vi.fn()));
    expect(screen.getByText('menu-enabled')).toBeInTheDocument();
  });

  it('dismisses the older toast, and its undo, when a newer pick lands', async () => {
    const dismissFirst = vi.fn();
    mocks.toast.mockImplementationOnce(() => ({ id: 't1', dismiss: dismissFirst, update: vi.fn() }));
    mocks.mutateAsync.mockResolvedValue(undefined);
    render(tree(vi.fn()));

    await act(async () => { fireEvent.click(screen.getByText('pick-beta')); });
    expect(mocks.toast).toHaveBeenCalledTimes(1);

    await act(async () => { fireEvent.click(screen.getByText('pick-gamma')); });
    expect(dismissFirst).toHaveBeenCalledTimes(1);
    expect(mocks.toast).toHaveBeenCalledTimes(2);
  });

  it('an undo pressed after the mode changed restores the preference, not the composer', async () => {
    mocks.preferences = {
      model_preference: { preferred_model: 'model-alpha', preferred_flash_model: 'model-flash' },
    };
    mocks.mutateAsync.mockImplementation(savePreference);
    const onModelChange = vi.fn();
    const { rerender } = render(tree(onModelChange, 'fast'));

    await act(async () => { fireEvent.click(screen.getByText('pick-beta')); });
    rerender(tree(onModelChange, 'ptc'));
    expect(onModelChange).toHaveBeenLastCalledWith('model-alpha');

    const undo = mocks.toast.mock.calls[0][0].action.props.onClick;
    await act(async () => { undo(); });

    expect(mocks.mutateAsync).toHaveBeenLastCalledWith({
      model_preference: { preferred_flash_model: 'model-flash' },
    });
    expect(onModelChange).toHaveBeenLastCalledWith('model-alpha');
  });

  it('an undo pressed after the preference changed elsewhere leaves the newer choice', async () => {
    mocks.mutateAsync.mockImplementation(savePreference);
    render(tree(vi.fn()));

    await act(async () => { fireEvent.click(screen.getByText('pick-beta')); });
    // Settings saves another model while the toast is still up.
    await savePreference({ model_preference: { preferred_model: 'model-gamma' } });

    const undo = mocks.toast.mock.calls[0][0].action.props.onClick;
    await act(async () => { undo(); });

    expect(mocks.mutateAsync).toHaveBeenCalledTimes(1);
  });
});

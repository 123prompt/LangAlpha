/**
 * A pick from the composer's model menu writes the account preference, so the
 * choice is kept in one place rather than copied onto the thread. These lock
 * which key it writes and what an undo has to restore.
 */
import { describe, it, expect } from 'vitest';
import { modelPickWrite } from '../chat-input.helpers';

describe('modelPickWrite', () => {
  it('writes the primary key in ptc mode', () => {
    expect(modelPickWrite('ptc', 'model-b', 'model-a', null)).toEqual({
      key: 'preferred_model',
      previous: 'model-a',
    });
  });

  it('writes nothing when the pick is already the resolved model', () => {
    expect(modelPickWrite('ptc', 'model-a', 'model-a', null)).toBeNull();
  });

  it('writes the flash key in fast mode, not the primary one', () => {
    expect(modelPickWrite('fast', 'model-c', 'model-a', 'model-b')).toEqual({
      key: 'preferred_flash_model',
      previous: 'model-b',
    });
  });

  it('treats an inherited flash model as the resolved one, so re-picking it writes nothing', () => {
    // No stored flash preference: the composer is showing the primary model.
    expect(modelPickWrite('fast', 'model-a', 'model-a', null)).toBeNull();
  });

  it('carries a null previous when flash was inheriting, so undo restores the inheritance', () => {
    expect(modelPickWrite('fast', 'model-c', 'model-a', null)).toEqual({
      key: 'preferred_flash_model',
      previous: null,
    });
  });

  it('carries a null previous when nothing was ever chosen', () => {
    expect(modelPickWrite('ptc', 'model-a', null, null)).toEqual({
      key: 'preferred_model',
      previous: null,
    });
  });

  it('defaults to the primary key when mode is unset', () => {
    expect(modelPickWrite(undefined, 'model-b', 'model-a', 'model-flash')).toEqual({
      key: 'preferred_model',
      previous: 'model-a',
    });
  });
});

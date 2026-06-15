import { describe, expect, it } from 'vitest';

import { stripNextAction } from '../src/ui/next-action.js';

describe('stripNextAction', () => {
  it('removes a complete footer block', () => {
    const text =
      'Hello! How can I help you today?\n\n<next_action complexity="low" summary="hide">\ndone\n</next_action>';
    expect(stripNextAction(text)).toBe('Hello! How can I help you today?');
  });

  it('removes multiple blocks', () => {
    const text =
      'First answer.\n<next_action complexity="low">x</next_action>\nMore.\n<next_action summary="show">y</next_action>';
    const out = stripNextAction(text);
    expect(out).not.toContain('next_action');
    expect(out).toContain('First answer.');
    expect(out).toContain('More.');
  });

  it('hides a streaming partial footer (opener only, no close yet)', () => {
    expect(stripNextAction('All set.\n<next_action complexity="low"')).toBe('All set.');
    expect(stripNextAction('All set.\n<next_a')).toBe('All set.');
    expect(stripNextAction('All set.\n<')).toBe('All set.');
  });

  it('leaves ordinary text and mid-sentence < untouched', () => {
    expect(stripNextAction('2 < 3 and 5 > 4')).toBe('2 < 3 and 5 > 4');
    expect(stripNextAction('Hello there')).toBe('Hello there');
  });

  it('handles empty input', () => {
    expect(stripNextAction('')).toBe('');
  });
});

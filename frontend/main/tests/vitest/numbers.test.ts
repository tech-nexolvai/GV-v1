import { describe, expect, it } from 'vitest';

import { splitNumbers } from '@/lib/numbers';

// Moved from the old Review list's library tests (removed 2026-10-28) with `splitNumbers` itself.
describe('splitNumbers', () => {
  it('splits numbers out of plain words so they can use the number face', () => {
    expect(splitNumbers('39 1/2 in != 41 5/8 in').filter((part) => part.number).map((part) => part.text)).toEqual(['39 1/2 in', '41 5/8 in']);
  });
});

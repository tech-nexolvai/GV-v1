import { describe, expect, it } from 'vitest';

import { widthNotAboveZero } from '@/lib/widths';

describe('widthNotAboveZero (#1182)', () => {
  it.each(['0', '0 in', '0"', ' 0.0 ', '0/16"', '-1 in', '−2"', '-1/16'])('refuses %s', (typed) => {
    expect(widthNotAboveZero(typed)).toBe(true);
  });

  it.each(['1/16"', '10 in', '0.5"', '21 3/8"', '610 mm', '', 'abc'])('lets %s through', (typed) => {
    expect(widthNotAboveZero(typed)).toBe(false);
  });
});

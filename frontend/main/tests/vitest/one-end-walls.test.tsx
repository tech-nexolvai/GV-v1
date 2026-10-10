import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { WallGlyph } from '@/components/results/wall-glyph';
import { wallLayoutLabel } from '@/components/measure/countertopRunChoices';
import { wallWords } from '@/lib/needs-you-queue';

// #1138: a countertop with a wall at its left end only, or its right end only. Synthetic data only.
describe('wall layouts: back wall and one end', () => {
  it('are named in words everywhere a layout is named, never as their code', () => {
    expect(wallWords('back_and_left')).toBe('Back wall and left end');
    expect(wallWords('back_and_right')).toBe('Back wall and right end');
    expect(wallLayoutLabel('back_and_left')).toBe('Back wall and left end');
    expect(wallLayoutLabel('back_and_right')).toBe('Back wall and right end');
  });

  it('the results glyph says the layout in words', () => {
    for (const [config, label] of [['back_and_left', 'back wall and left end'], ['back_and_right', 'back wall and right end']]) {
      const html = renderToStaticMarkup(createElement(WallGlyph, { layout: { config, label, source: 'reviewer' } }));
      expect(html).toContain(label);
      expect(html).not.toContain(config);
    }
  });
});

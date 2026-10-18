// @vitest-environment jsdom
/**
 * #1547: products are data. The chips, the "+" menu's Focus options, the words a full check is named
 * with and the findings' groups all come from `GET /products` (the product registry). A product
 * added only to the registry (here `dummy_widget`) appears everywhere with no frontend change; the
 * products not ready yet are shown greyed, "coming later". Synthetic data only.
 */
import { fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ProductChips } from '@/components/chat/chat-full-check';
import { checkedWords, effectiveProducts, pickable, productsWords, readProducts, scopeSentence, toggleProduct } from '@/lib/chat-full-check';
import { categoriesPresent, categoryWords, groupFindings } from '@/lib/chat-findings';
import { plusAction, plusActions } from '@/lib/chat-plus-actions';
import { findingsCsv } from '@/lib/chat-trust';
import { loadProducts, productCatalog, productPlural, setProductCatalog } from '@/lib/products';
import { finding } from './fixtures/chat';
import { DUMMY, PRODUCTS } from './fixtures/products';

const WITH_DUMMY = [...PRODUCTS, DUMMY];

describe('a product added only to the registry', () => {
  beforeEach(() => setProductCatalog(WITH_DUMMY));

  it('gets a chip, a Focus option and its words', () => {
    expect(pickable()).toEqual(['countertop', 'cabinet', 'dummy_widget']);
    expect(productsWords(['dummy_widget', 'countertop'])).toBe('Countertops + Dummy widgets');
    expect(toggleProduct(['dummy_widget'], 'countertop')).toEqual(['countertop', 'dummy_widget']);
    expect(effectiveProducts(null, null, 'dummy_widget')).toEqual(['dummy_widget']);

    const focus = plusActions().filter((action) => action.group === 'focus').map((action) => action.label);
    expect(focus.slice(0, 4)).toEqual(['Countertops only', 'Cabinets only', 'Dummy widgets only', 'All products']);
    expect(plusAction('all_products')?.run).toEqual({ kind: 'products', products: ['countertop', 'cabinet', 'dummy_widget'] });

    render(<ProductChips products={['dummy_widget']} onToggle={() => {}} />);
    const chips = screen.getByRole('group', { name: 'Products to check' });
    expect(within(chips).getAllByRole('button').map((chip) => chip.textContent)).toEqual(['Countertop', 'Cabinet', 'Dummy widget']);
    expect(within(chips).getByRole('button', { name: 'Dummy widget' }).getAttribute('aria-pressed')).toBe('true');
  });

  it('the quiet All chip picks every active product, the made-up one too (any two, any three)', () => {
    const picked: string[][] = [];
    const { rerender } = render(<ProductChips products={['countertop', 'dummy_widget']} onToggle={() => {}} onAll={() => picked.push(pickable())} />);
    const all = screen.getByRole('button', { name: 'All' });
    expect(all.getAttribute('aria-pressed')).toBe('false');
    fireEvent.click(all);
    expect(picked).toEqual([['countertop', 'cabinet', 'dummy_widget']]);
    rerender(<ProductChips products={['countertop', 'cabinet', 'dummy_widget']} onToggle={() => {}} onAll={() => {}} />);
    expect(screen.getByRole('button', { name: 'All' }).getAttribute('aria-pressed')).toBe('true');
    expect(checkedWords(['countertop', 'dummy_widget'])).toBe('Checked: Countertops + Dummy widgets');
    expect(scopeSentence(['countertop', 'dummy_widget'])).toBe('Only countertops and dummy widgets are checked; cabinets are left out.');
    expect(checkedWords(['cabinet', 'dummy_widget', 'countertop'])).toBe('Checked: All products');
  });

  it('groups its findings under its own heading, before Other', () => {
    const findings = [
      finding('a', { category: 'other', page: 1 }),
      finding('b', { category: 'dummy_widget', page: 4 }),
      finding('c', { category: 'countertop', page: 2 }),
    ];
    expect(groupFindings(findings).map((group) => group.category)).toEqual(['countertop', 'dummy_widget', 'other']);
    expect(categoriesPresent(findings).map((category) => categoryWords(category))).toEqual(['Countertops', 'Dummy widgets', 'Other']);
    const csv = findingsCsv(findings);
    expect(csv.indexOf('Countertops')).toBeLessThan(csv.indexOf('Dummy widgets'));
    expect(csv.indexOf('Dummy widgets')).toBeLessThan(csv.lastIndexOf('Other'));
  });
});

describe('products not ready yet', () => {
  it('are one greyed "coming later" chip and greyed Focus options, never picked', () => {
    render(<ProductChips products={['countertop']} onToggle={() => {}} />);
    const later = screen.getByText('6 coming later');
    expect(later.getAttribute('aria-disabled')).toBe('true');
    expect(later.getAttribute('title')).toBe('Coming later: Lighting, Seating, Case goods, Mirrors, Accessories, Artwork');
    expect(plusAction('product:seating')).toMatchObject({ hint: 'Coming later', run: { comingLater: true } });
    expect(pickable()).not.toContain('lighting');
  });

  it('are left out of a remembered pick', () => {
    window.localStorage.setItem('gv-chat-products:p1', JSON.stringify(['lighting', 'cabinet', 'retired']));
    expect(readProducts(window.localStorage, 'p1')).toEqual(['cabinet']);
    // A set of a product not ready yet is checked for every active product (as the server does).
    expect(effectiveProducts(null, null, 'lighting')).toEqual(['countertop', 'cabinet']);
  });

  it('old answers keep their words: a category no product has any more still shows', () => {
    const old = [finding('x', { category: 'retired_thing' })];
    expect(categoriesPresent(old)).toEqual(['retired_thing']);
    expect(categoryWords('retired_thing')).toBe('Retired things');
  });
});

describe('loading the products', () => {
  afterEach(() => setProductCatalog(PRODUCTS));

  it('reads GET /products once and falls back to readable ids meanwhile', async () => {
    setProductCatalog([]);
    expect(productPlural('case_goods')).toBe('Case goods');
    const fetch = vi.fn(async () => new Response(JSON.stringify(WITH_DUMMY), { status: 200, headers: { 'content-type': 'application/json' } }));
    vi.stubGlobal('fetch', fetch);
    await Promise.all([loadProducts(), loadProducts()]);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(String((fetch.mock.calls[0] as unknown[])[0])).toMatch(/\/api\/v1\/products$/);
    expect(productCatalog().map((product) => product.id)).toContain('dummy_widget');
  });
});

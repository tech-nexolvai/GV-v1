/**
 * The product registry as `GET /products` lists it (#1547), for tests: countertop and cabinet
 * active, the client's six other products listed but inactive. Mirrors `vocabulary/products.yaml`'s
 * ids, words and active flags; the checklists are cut short.
 */
import type { ProductInfo } from '@/lib/products';

function product(id: string, label: string, plural: string, active: boolean, published_checks = 0): ProductInfo {
  return {
    id,
    label,
    plural,
    description: `${plural} (test)`,
    active,
    draft: !active,
    check_kinds: ['dimension_match', 'global_rules'],
    category_label: plural,
    checklist: [{ name: 'sizes', text: 'Sizes against the architect’s.' }],
    published_checks,
  };
}

export const PRODUCTS: ProductInfo[] = [
  product('countertop', 'Countertop', 'Countertops', true, 8),
  product('cabinet', 'Cabinet', 'Cabinets', true, 2),
  product('lighting', 'Lighting', 'Lighting', false),
  product('seating', 'Seating', 'Seating', false),
  product('case_goods', 'Case good', 'Case goods', false),
  product('mirrors', 'Mirror', 'Mirrors', false),
  product('accessories', 'Accessory', 'Accessories', false),
  product('artwork', 'Artwork', 'Artwork', false),
];

/** A product added only to the registry: it must appear everywhere with no frontend change. */
export const DUMMY: ProductInfo = product('dummy_widget', 'Dummy widget', 'Dummy widgets', true, 1);

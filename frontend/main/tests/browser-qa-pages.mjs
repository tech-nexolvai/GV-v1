// Invented, explicitly synthetic page data. Imported only by the isolated test server.
export const pageRules = [
  {
    rule_id: 'SYNTHETIC-CABINET-001', name: 'Synthetic cabinet check', product_type: 'CABINET',
    check_type: 'equals', severity: 'FLAG', production_ready: false, version: '1',
    snapshot_id: 'synthetic-cabinet-snapshot-not-a-published-rule', published_versions: 1,
    unconfirmed_tolerances: 0, release_note: 'Synthetic UI fixture only; not a published production rule.',
  },
  {
    rule_id: 'SYNTHETIC-COUNTERTOP-001', name: 'Synthetic countertop check', product_type: 'COUNTERTOP',
    check_type: 'minimum', severity: 'FLAG', production_ready: false, version: '2',
    snapshot_id: 'synthetic-countertop-snapshot-not-a-published-rule', published_versions: 2,
    unconfirmed_tolerances: 1, release_note: 'Synthetic warning and long-field layout fixture; no production meaning.',
  },
];

export const pageSettings = { settings: [
  {
    name: 'synthetic_width', scope: 'global', rule_ids: ['SYNTHETIC-CABINET-001'],
    rulebook_default: '20 1/2 in', rulebook_note: 'Synthetic example only.',
    company_value: null, company_set_by: null, company_set_at: null,
    in_use: '20 1/2 in', in_use_from: 'rulebook',
  },
  {
    name: 'synthetic_depth', scope: 'project', rule_ids: ['SYNTHETIC-COUNTERTOP-001'],
    rulebook_default: null, rulebook_note: null,
    company_value: null, company_set_by: null, company_set_at: null,
    in_use: null, in_use_from: null,
  },
] };

export function pageFixture(path, mode) {
  if (mode === 'partial' && (path.endsWith('/review-sessions') || path.endsWith('/packages/00000000-0000-4000-8000-000000000101/findings/summary'))) {
    return { status: 503, body: { error: 'synthetic_partial_outage', message: 'Synthetic supplementary data unavailable; document records still exist.', request_id: 'SYNTHETIC_UI_QA' } };
  }
  const known = path === '/api/v1/rules' || path === '/api/v1/company-settings' || path.endsWith('/packages');
  if (mode === 'error' && known) return { status: 503, body: { error: 'synthetic_page_outage', message: 'Synthetic page load failure. No backend was contacted.', request_id: 'SYNTHETIC_UI_QA' } };
  if (path === '/api/v1/rules') return { status: 200, body: mode === 'empty' ? [] : pageRules };
  if (path === '/api/v1/company-settings') return { status: 200, body: mode === 'empty' ? { settings: [] } : pageSettings };
  if (mode === 'empty' && path.endsWith('/packages')) return { status: 200, body: { items: [], next_cursor: null, limit: 50, ordering: 'synthetic-test-order' } };
  return null;
}

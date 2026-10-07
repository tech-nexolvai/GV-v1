import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';
import { DocumentRecord, DocumentDetails } from '../src/pages/DocumentRecord.js';
import { readFileSync } from 'node:fs';
import type { DocumentRow } from '../src/pages/documentRows.js';

const row: DocumentRow = {
  document: {
    id: 'synthetic-document-id-preserved-in-full', project_id: 'synthetic-project-id-preserved-in-full',
    current_revision_id: 'synthetic-revision-id-preserved-in-full', current_revision_number: 2,
    vendor: 'Synthetic vendor <untrusted>', state: 'AWAITING_REVIEW', created_at: '2026-10-03T10:00:00Z',
    product_type: null,
  },
  reviewer: 'Synthetic reviewer', counts: null, countsError: 'summary unavailable',
};
const before = JSON.stringify(row);
const html = renderToStaticMarkup(<><DocumentRecord row={row} /><DocumentDetails row={row} reviewerUnavailable={false} /></>);
for (const value of [row.document.id, row.document.project_id, row.document.current_revision_id, row.reviewer!]) {
  assert.ok(html.includes(value), `retain complete record field: ${value}`);
}
assert.match(html, /Synthetic vendor &lt;untrusted&gt;/, 'vendor text remains escaped, never markup');
assert.match(html, /Revision 2/);
assert.match(html, /<details[^>]*><summary>Technical record details/);
assert.doesNotMatch(html, /<details[^>]* open/, 'technical details are collapsed initially');
assert.ok(html.indexOf('Synthetic vendor') < html.indexOf('Document ID'), 'lead with human-readable vendor');
assert.equal(JSON.stringify(row), before, 'presentation must not mutate backend records');

const missing: DocumentRow = { ...row, document: { ...row.document, vendor: null }, reviewer: null };
const unavailable = renderToStaticMarkup(<><DocumentRecord row={missing} /><DocumentDetails row={missing} reviewerUnavailable /></>);
assert.match(unavailable, /Untitled document set/);
assert.match(unavailable, /<dd>Unavailable<\/dd>/);
assert.doesNotMatch(unavailable, /<dd>Not listed<\/dd>/);
const notListed = renderToStaticMarkup(<DocumentDetails row={missing} reviewerUnavailable={false} />);
assert.match(notListed, /<dd>Not listed<\/dd>/);
assert.match(notListed, /<dt>Category<\/dt><dd>Not provided<\/dd>/, 'do not invent missing category');
const countertop: DocumentRow = { ...row, document: { ...row.document, product_type: 'countertop' } };
assert.match(
  renderToStaticMarkup(<DocumentDetails row={countertop} reviewerUnavailable={false} />),
  /<dt>Category<\/dt><dd>Countertop \(only its checks are run\)<\/dd>/,
  'the product the reviewer chose at upload (#994)',
);
// DOM order keeps the action and results above metadata at every breakpoint.
const page = readFileSync('src/pages/PackagesPage.tsx', 'utf8');
assert.ok(page.indexOf('Open review\n') < page.indexOf('<DocumentDetails row='));
assert.ok(page.indexOf('<DocumentResults counts=') < page.indexOf('<DocumentDetails row='));
assert.match(page, /<ul className="document-cards" role="list" aria-label="Drawing reviews">/);
console.log('document-record: complete identifiers, escaped vendor, revision, disclosure, unavailable vs absent reviewer, immutable records passed');

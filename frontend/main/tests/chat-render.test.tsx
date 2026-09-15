import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';

import { ChatMarkdown } from '../src/components/chat/ChatMarkdown.js';
import { findingsTableMarkdown } from '../src/components/chat/findingsTable.js';
import type { Finding } from '../src/data/types.js';

function render(text: string): string {
  return renderToStaticMarkup(<ChatMarkdown text={text} />);
}

const finding: Finding = {
  id: 'finding-1',
  check_id: 'CT-DEPTH-001',
  name: 'Countertop depth',
  outcome: 'FAIL',
  severity: 'FLAG',
  reviewer_action: null,
  recorded_operands: [
    {
      name: 'approved_depth',
      value: '25 in',
      source: 'ARCH',
      status: 'APPROVED',
      hasEvidence: true,
      documentRole: 'ARCH',
    },
    {
      name: 'vendor_depth',
      value: '25 1/2 in',
      source: 'SHOP',
      status: 'APPROVED',
      hasEvidence: true,
      documentRole: 'SHOP',
    },
  ],
  arch_evidence: {
    canonical_observation_id: 'obs-a',
    page: 12,
    polygon: [[0, 0]],
    semantic_type: 'countertop_depth',
  },
  shop_evidence: {
    canonical_observation_id: 'obs-s',
    page: 13,
    polygon: [[0, 0]],
    semantic_type: 'countertop_depth',
  },
};

const table = findingsTableMarkdown([finding]);
const html = render(`**Recorded findings table**\n${table}\n\nOpen \`Evidence & facts\`.`);

assert.match(html, /<table class="chat-table">/);
assert.match(html, /<strong>Recorded findings table<\/strong>/);
assert.match(html, /<code>Evidence &amp; facts<\/code>/);
assert.match(html, /<th scope="col">Reading<\/th>/);
assert.match(html, /<td>25 1\/2 in<\/td>/);
assert.match(html, /<td>Shop p\.13<\/td>/);
assert.match(html, /<td>25 in<\/td>/);
assert.match(html, /<td>Fail<\/td>/);
assert.doesNotMatch(html, /\| Check \| Reading \|/);

console.log('chat-render component test passed');

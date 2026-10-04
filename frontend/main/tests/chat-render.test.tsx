import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';

import { ChatMarkdown } from '../src/components/chat/ChatMarkdown.js';
import { PromptSuggestions } from '../src/components/chat/PromptSuggestions.js';
import { ReviewPackageDetails } from '../src/pages/ReviewPackageDetails.js';
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
    document_version_id: 'doc-a',
    page: 12,
    polygon: [[0, 0]],
    semantic_type: 'countertop_depth',
  },
  shop_evidence: {
    canonical_observation_id: 'obs-s',
    document_version_id: 'doc-s',
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
// The reviewer-facing wording, not the engine's — `src/data/outcomeLabels.ts` is the one list,
// and `tests/test_outcome_labels.py` holds it to what the backend narration says.
assert.match(html, /<td>Needs correction<\/td>/);
assert.doesNotMatch(html, /<td>Fail<\/td>/);
assert.doesNotMatch(html, /\| Check \| Reading \|/);

const questions = ['Show all findings', 'Show FAIL findings', 'Show findings needing review',
  'Which sheet has the failure?', 'Why did this fail?'];
let sends = 0;
const suggestions = renderToStaticMarkup(<PromptSuggestions prompts={questions} onSend={() => { sends += 1; }} />);
assert.match(suggestions, /aria-expanded="false"/);
const listId = suggestions.match(/aria-controls="([^"]+)"/)?.[1];
assert.ok(listId && suggestions.includes(`id="${listId}"`));
assert.equal(suggestions.match(/class="chat-input-area__quick-btn"/g)?.length, 5);
for (const question of questions) assert.equal(suggestions.split(question).length - 1, 1);
assert.equal(renderToStaticMarkup(<PromptSuggestions prompts={[]} onSend={() => {}} />), '');
const disabledSuggestions = renderToStaticMarkup(<PromptSuggestions prompts={questions} disabled onSend={() => { sends += 1; }} />);
assert.equal(disabledSuggestions.match(/disabled=""/g)?.length, 5);
assert.equal(sends, 0, 'rendering suggestions does not send a question');

const details = renderToStaticMarkup(
  <ReviewPackageDetails packageId="package-a" projectId="project-b">
    <ol><li>Upload</li><li>Review</li></ol>
  </ReviewPackageDetails>,
);
assert.match(details, /Details &amp; steps/);
assert.match(details, /package-a/);
assert.match(details, /project-b/);
assert.match(details, /Upload/);
assert.doesNotMatch(details, /<details[^>]* open=/);

console.log('chat-render component test passed');

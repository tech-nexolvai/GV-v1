import assert from 'node:assert/strict';

import { parseSseFrames } from '../src/api/sse.js';
import { explanationUnavailable, factsMessage, replyMessage, withStreamStage } from '../src/components/chat/chatReply.js';
import type { ReviewerChatReply } from '../src/api/chatStreamTypes.js';
import type { Finding } from '../src/data/types.js';

// --- frames -----------------------------------------------------------------------------------

{
  // A frame split across two network chunks: nothing is parsed until it is complete.
  const first = parseSseFrames('event: facts\ndata: {"answer":"Show');
  assert.deepEqual(first.frames, [], 'a half-written frame is not parsed');
  const second = parseSseFrames(first.rest + 'ing 1 of 2"}\n\nevent: done\ndata: {}\n\n');
  assert.deepEqual(second.frames, [
    { event: 'facts', data: { answer: 'Showing 1 of 2' } },
    { event: 'done', data: {} },
  ]);
  assert.equal(second.rest, '');
}

{
  // FastAPI's keep-alive comment every 15 seconds, CRLF line endings, and the SSE default event.
  const parsed = parseSseFrames(': ping\r\n\r\ndata: {"a":1}\r\n\r\n');
  assert.deepEqual(parsed.frames, [{ event: 'message', data: { a: 1 } }], 'comments are skipped');
}

{
  // Multiple data lines are one value joined by newlines.
  const parsed = parseSseFrames('event: x\ndata: [1,\ndata: 2]\n\n');
  assert.deepEqual(parsed.frames, [{ event: 'x', data: [1, 2] }]);
}

// --- messages ---------------------------------------------------------------------------------

function finding(id: string, outcome: Finding['outcome']): Finding {
  return { id, check_id: `RULE-${id}`, name: id, outcome, severity: 'FLAG', reviewer_action: null } as Finding;
}

const source = [finding('a', 'FAIL'), finding('b', 'PASS'), finding('c', 'REVIEW_REQUIRED')];

{
  const shown = factsMessage(
    { answer: 'Showing 2 of 3 recorded findings.', finding_ids: ['a', 'c', 'unknown'], total: 3, narrating: true },
    source,
    'msg-1',
    '2026-09-27T00:00:00Z',
  );
  assert.deepEqual(shown.findings?.map((item) => item.id), ['a', 'c'], 'unknown ids are dropped, not invented');
  assert.equal(shown.findings?.[0], source[0], 'the same loaded objects, so evidence and actions still work');
  assert.equal(shown.narrating, true, 'the thread can say an explanation is coming');
  assert.equal(shown.narration, undefined, 'no provenance badge before there is prose');
  assert.equal(shown.content, 'Showing 2 of 3 recorded findings.');
  assert.equal(shown.streamStage, undefined, 'the facts frame cannot claim an unreported stage');
  const staged = withStreamStage(shown, 'narrating');
  assert.equal(staged.streamStage, 'narrating', 'the emitted stage is kept verbatim');
  assert.equal(shown.streamStage, undefined, 'the original recorded facts are unchanged');
}

const llmReply = {
  answer: 'Showing 2 of 3 recorded findings.',
  mode: 'llm',
  model_id: 'model-x',
  fallback_reason: null,
  summary: 'Two findings need you.',
  findings: [
    { finding_id: 'a', text: 'The vendor depth is half an inch deeper.' },
    { finding_id: 'c', text: 'Confirm the filler.' },
  ],
} as unknown as ReviewerChatReply;

{
  const final = replyMessage(llmReply, source, 'msg-1', '2026-09-27T00:00:01Z');
  assert.equal(final.id, 'msg-1', 'the reply replaces the facts message in place');
  assert.equal(final.narrating, undefined, 'the pending line is gone');
  assert.equal(final.content, '**Two findings need you.**\n\nShowing 2 of 3 recorded findings.');
  assert.deepEqual(final.narratives, {
    a: 'The vendor depth is half an inch deeper.',
    c: 'Confirm the filler.',
  });
  assert.equal(final.narration?.mode, 'llm');
}

{
  const empty = replyMessage(
    { ...llmReply, mode: 'structured_fallback', summary: null, findings: [] } as unknown as ReviewerChatReply,
    source,
    'msg-2',
    '2026-09-27T00:00:02Z',
  );
  assert.equal(empty.narration, undefined, 'nothing selected: no badge announcing a disclosure about nothing');
}

{
  const shown = factsMessage(
    { answer: 'Showing 1 of 3.', finding_ids: ['a'], total: 3, narrating: true },
    source,
    'msg-3',
    '2026-09-27T00:00:03Z',
  );
  const failed = explanationUnavailable(shown, 'The explanation could not be produced.');
  assert.equal(failed.findings, shown.findings, 'a failed explanation keeps the findings on screen');
  assert.equal(failed.narrating, false);
  assert.equal(failed.narration?.mode, 'structured_fallback');
}

console.log('chat stream: all assertions passed');

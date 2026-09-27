/**
 * The stream, parsed from a transcript the running server actually produced.
 *
 * What this adds over `chat-stream.test.ts`: that one writes its own frames, so it verifies the
 * parser against itself. This one verifies the parser against the server — the event names, the
 * order they arrive in, and the fields each payload carries.
 *
 * What it does not cover: the `stage` event. It fires "only when a provider is actually about to be
 * called", and no findings language model is configured on this machine, so the live run went
 * straight from `facts` to `narration`. That path needs Bedrock narration configured — and for
 * Anthropic, #665.
 */
import assert from 'node:assert/strict';

import { parseSseFrames } from '../src/api/sse.js';
import { LIVE_SSE_TRANSCRIPT } from './liveSse.js';

const { frames } = parseSseFrames(LIVE_SSE_TRANSCRIPT);

// **The order is the point of the whole PR**: the findings a reviewer asked for arrive before any
// model has written a word, and the explanation replaces the pending line afterwards. An ordering
// regression would put the prose first and make the stream pointless.
assert.deepEqual(
  frames.map((frame) => frame.event),
  ['facts', 'narration', 'done'],
  'the server did not send facts, then narration, then done',
);

const facts = frames[0].data as Record<string, unknown>;
assert.ok(Array.isArray(facts.finding_ids), 'facts carries no finding_ids');
assert.equal((facts.finding_ids as unknown[]).length, 1, 'the question selected one finding');
assert.equal(facts.total, 9, 'the package has nine findings');
// No model has been consulted at this point, and the payload has to say so.
assert.equal(facts.narrating, false);
assert.ok(typeof facts.answer === 'string' && facts.answer.length > 0);

const narration = frames[1].data as Record<string, unknown>;
assert.ok(['llm', 'structured_fallback'].includes(narration.mode as string));
assert.ok(Array.isArray(narration.findings), 'narration carries no per-finding text');
// The same finding the facts event selected, so a row and its explanation cannot disagree.
assert.deepEqual(
  (narration.findings as { finding_id: string }[]).map((item) => item.finding_id),
  facts.finding_ids,
);

// An error frame must never carry exception text. There is none here, and this pins that a future
// transcript with one is a failure rather than a quiet change.
assert.ok(!frames.some((frame) => frame.event === 'error'), 'the live run produced an error frame');

console.log(`live chat stream: ${frames.length} frames in the documented order`);

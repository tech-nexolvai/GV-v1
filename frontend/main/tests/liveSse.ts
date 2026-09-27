/**
 * A real server-sent-event transcript, captured 2026-09-27.
 *
 * Not a fixture: this is what `POST .../chat/stream` actually sent, from a server running against a
 * real database with nine findings from the seeded demo package, asked "why did this fail?".
 *
 * `chat-stream.test.ts` parses hand-written frames, which tests the parser and cannot catch the one
 * thing this does: the server's event names or payload fields drifting from what the client reads.
 */

export const LIVE_SSE_TRANSCRIPT =
  'event: facts\ndata: {"answer":"Showing 1 of 9 recorded findings. The explanation below is grounded in the deterministic run.","finding_ids":["17dc5ac9-b947-4798-8947-74551b590651"],"total":9,"narrating":false}\n\nevent: narration\ndata: {"answer":"Showing 1 of 9 recorded findings. The explanation below is grounded in the deterministic run.","mode":"structured_fallback","model_id":null,"fallback_reason":"no findings language model is configured","summary":null,"findings":[{"finding_id":"17dc5ac9-b947-4798-8947-74551b590651","text":"Countertop depth verification \u2014 Needs correction (CT-DEPTH-001). 25 1/4 in != 25 1/2 in Countertop depth from vendor: 25 1/4 in. Evidence page: 1. Recorded comparison: 25 1/4 in != 25 1/2 in. Unit: in. Evidence pages: 1. Next: review the vendor drawing against the approved design."}]}\n\nevent: done\ndata: {}\n\n';

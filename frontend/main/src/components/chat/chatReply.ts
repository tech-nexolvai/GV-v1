/**
 * The chat messages a reply becomes, built without React so they can be tested.
 *
 * Moved out of `ReviewPage` when the reply started arriving in two parts: the findings first
 * (`factsMessage`), then the guarded narration that replaces it (`replyMessage`). Both must show the
 * same findings in the same objects, so both map ids back through the page's loaded findings.
 */

import type { ChatStreamFacts, ReviewerChatReply } from '../../api/chatStreamTypes';
import type { ChatMessage, Finding } from '../../data/types';

/**
 * The backend's ids are the authoritative run scope. They are mapped back to the already-loaded
 * findings so evidence and actions remain the same grounded objects the rest of the reviewer loop
 * uses. An id the page does not have is dropped rather than invented.
 */
function byIds(ids: readonly string[], source: readonly Finding[]): Finding[] {
  const byId = new Map(source.map((finding) => [finding.id, finding]));
  return ids.flatMap((id) => {
    const finding = byId.get(id);
    return finding ? [finding] : [];
  });
}

/**
 * The first part of a streamed reply: the findings that answer the question, before any model has
 * written a word. The text is the deterministic intro. `narrating` says an explanation is on its
 * way, so the thread can say so; it is not a timer, it is the server's own statement.
 */
export function factsMessage(
  facts: ChatStreamFacts,
  source: readonly Finding[],
  id: string,
  timestamp: string,
): ChatMessage {
  return {
    id,
    role: 'assistant',
    content: facts.answer,
    timestamp,
    findings: byIds(facts.finding_ids, source),
    narrating: facts.narrating,
  };
}

/** The complete reply, as `/chat` returns it and as the stream's `narration` frame carries it. */
export function replyMessage(
  response: ReviewerChatReply,
  source: readonly Finding[],
  id: string,
  timestamp: string,
): ChatMessage {
  const matched = byIds(
    response.findings.map((item) => item.finding_id),
    source,
  );
  const matchedIds = new Set(matched.map((finding) => finding.id));
  // Kept per finding, not joined into one block of prose: each opens under its own row.
  const narratives = Object.fromEntries(
    response.findings
      .filter((item) => matchedIds.has(item.finding_id))
      .map((item) => [item.finding_id, item.text]),
  );
  // **Two different things wore the same message.** "AI narration is off on this package" was
  // shown whenever the mode was `structured_fallback`, including when the question simply
  // matched no findings — ask "why did this fail?" about a package with no failures and the
  // screen announced that the AI was switched off. It was not: Bedrock was configured,
  // reachable, and had nothing to narrate because nothing had been selected for it.
  //
  // An empty selection is the honest, common case, and saying so is a better answer than an
  // apology for a capability that is working.
  const nothingSelected = response.findings.length === 0;
  // **The reason is for us, not for the reviewer.**
  //
  // This printed `response.fallback_reason` verbatim, and a reviewer asking "why did this
  // fail?" was shown a Pydantic ValidationError quoting its own documentation URL. That is a
  // developer's diagnostic in a reviewer's face: it reads as the product breaking, when what
  // actually happened is the safety net working exactly as designed — the narration was
  // refused and the deterministic findings, which are the audit record anyway, were shown
  // instead. The reason is kept on the badge as a data attribute, out of the prose.
  //
  // **Minimal text.** The message carries the answer and, in LLM mode, the guarded overview.
  // The findings table is the record; the narration badge says which source wrote the prose;
  // each narrative opens under its own row.
  const overview = response.mode === 'llm' && response.summary ? `**${response.summary}**\n\n` : '';
  return {
    id,
    role: 'assistant',
    content: `${overview}${response.answer}`,
    timestamp,
    findings: matched,
    narratives,
    // Omitted when nothing was selected, so no provenance badge is rendered. The badge exists
    // to say which of two sources wrote the prose a reviewer is reading; where there is no
    // prose, announcing that a model did not write it is a disclosure about nothing, and it
    // read as a fault report.
    narration: nothingSelected
      ? undefined
      : {
          mode: response.mode === 'llm' ? 'llm' : 'structured_fallback',
          modelId: response.model_id ?? undefined,
          fallbackReason: response.fallback_reason ?? null,
        },
  };
}

/**
 * The findings already on screen stay; only the explanation is missing.
 *
 * Used when the stream fails *after* its facts arrived. The table the reviewer is looking at is the
 * recorded run and is still correct, so it is kept rather than replaced by an error.
 */
export function explanationUnavailable(message: ChatMessage, reason: string): ChatMessage {
  return {
    ...message,
    narrating: false,
    narration: { mode: 'structured_fallback', fallbackReason: reason },
  };
}

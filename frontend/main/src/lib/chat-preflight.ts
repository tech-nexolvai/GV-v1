import type { ChatFullCheckPlan, ChatPreflightLine, ChatSupervisor, PreflightWho } from '@/lib/chat-api';

/**
 * The pre-flight health report's words (code, $0; the screen part is
 * `components/chat/chat-preflight.tsx`). Each line says what was found; its second line says
 * whether the check goes on and who acts.
 */

const WHO_WORDS: Record<PreflightWho, string> = {
  you: 'You',
  GV: 'GV',
  'the vendor': 'The vendor',
  'the architect': 'The architect',
  nobody: 'Nobody',
};

const LEVEL_ORDER: Record<ChatPreflightLine['level'], number> = { stop: 0, warn: 1, ok: 2, note: 3 };

/** "Goes on · GV: Send the architect's drawings." / "Stops before spending · You: …"; null for a plain fact. */
export function preflightMeta(line: ChatPreflightLine): string | null {
  if (line.level === 'ok' && line.who === 'nobody' && !line.todo) return null;
  const goes = line.goes_on ? 'Goes on' : 'Stops before spending';
  if (line.who === 'nobody') return `${goes} · Nobody needs to act`;
  const who = WHO_WORDS[line.who] ?? line.who;
  return line.todo ? `${goes} · ${who}: ${line.todo}` : `${goes} · ${who} to act`;
}

/** Stops first, then warnings, then the plain facts, then the notes (each kept in its order). */
export function preflightOrder(lines: readonly ChatPreflightLine[]): ChatPreflightLine[] {
  return lines.map((line, at) => ({ line, at })).sort((a, b) => LEVEL_ORDER[a.line.level] - LEVEL_ORDER[b.line.level] || a.at - b.at).map(({ line }) => line);
}

/** Whether the plan's pre-flight report would pause the check (Run then reads "Run anyway"). */
export function preflightStops(plan: ChatFullCheckPlan | null | undefined): boolean {
  return (plan?.preflight?.stops?.length ?? 0) > 0;
}

/** Whether the popover should ask for the plan again (code is still working out the pairs). */
export function preflightWorking(plan: ChatFullCheckPlan | null | undefined): boolean {
  return plan?.preflight?.state === 'working';
}

/** The supervisor's one sentence, as shown ("A second look: …"); null when it said nothing. */
export function supervisorWords(look: ChatSupervisor | null | undefined): string | null {
  if (!look?.why || look.verdict === 'not_run' || look.verdict === 'ok') return null;
  return `A second look before the check: ${look.why}`;
}

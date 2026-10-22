/**
 * One word per meaning on the reviewer's screens (#1712, UI redesign step 1). The chat, the shell and
 * the tracker take their words from `@/lib/words`; this test fails when one of the retired words is
 * written into their code again (comments do not count: only what can reach the screen).
 */
import { readdirSync, readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

import { chatListWords, fallbackWords, footerWords, modelLabel } from '@/lib/chat-messages';
import { shortModelLabel } from '@/lib/chat-settings';
import { modelName } from '@/lib/usage';
import { sidebarWords } from '@/lib/set-check';
import { BANNED_WORDS, COMPOSER_NOTE, COMPOSER_PLACEHOLDER, OUTCOME_WORDS, roundTagWords } from '@/lib/words';

const SRC = resolve(__dirname, '../../src');

/** The reviewer-facing code: the chat, the shell, the tracker, the Documents table. The words module itself holds the list. */
function reviewerFiles(): string[] {
  const inDir = (dir: string, test: (name: string) => boolean) =>
    readdirSync(join(SRC, dir)).filter(test).map((name) => join(SRC, dir, name));
  return [
    ...inDir('components/chat', (name) => name.endsWith('.tsx')),
    ...inDir('components/shell', (name) => name.endsWith('.tsx')),
    ...inDir('components/documents', (name) => name.endsWith('.tsx')),
    ...inDir('lib', (name) => /^chat-.*\.ts$/.test(name) || name === 'tracker.ts' || name === 'set-check.ts'),
    join(SRC, 'pages/ChatPage.tsx'),
    join(SRC, 'pages/TrackerPage.tsx'),
  ];
}

/** Every source file under src (#1756: no screen anywhere names the model maker's assistant). */
function allSourceFiles(dir = SRC): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return allSourceFiles(path);
    return /\.tsx?$/.test(entry.name) ? [path] : [];
  });
}

/** The code without its comments, so explaining an old word in a comment is fine. */
function withoutComments(code: string): string {
  return code
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/\{\s*\/\*[\s\S]*?\*\/\s*\}/g, '')
    .replace(/(^|[\s;{}()])\/\/[^\n]*/g, '$1');
}

describe('reviewer words (#1712)', () => {
  it('no reviewer screen writes a retired word', () => {
    const found: string[] = [];
    for (const file of reviewerFiles()) {
      const code = withoutComments(readFileSync(file, 'utf8'));
      for (const banned of BANNED_WORDS) {
        const match = banned.pattern.exec(code);
        if (match) found.push(`${file.replace(SRC, 'src')}: "${match[0]}" (${banned.instead})`);
      }
    }
    expect(found).toEqual([]);
  });

  it('keeps the three outcome words and the product’s own voice', () => {
    expect(OUTCOME_WORDS).toEqual({ wrong: 'Wrong', right: 'Right', notCompared: 'Not compared' });
    expect(COMPOSER_PLACEHOLDER).toBe('Ask about these drawings…');
    expect(COMPOSER_NOTE).toBe('GV Review can be wrong. Every red mark shows its proof.');
    for (const words of [COMPOSER_PLACEHOLDER, COMPOSER_NOTE]) {
      for (const banned of BANNED_WORDS) expect(banned.pattern.test(words)).toBe(false);
    }
  });

  it('says the round-2 words the letter says (Closed / Previous comment not addressed / New)', () => {
    expect(roundTagWords('Still wrong')).toBe('Previous comment not addressed');
    expect(roundTagWords('New in round 2')).toBe('New');
    expect(roundTagWords('New in round 3')).toBe('New');
    // Everything is new in the first round: the tag says nothing there.
    expect(roundTagWords('New in round 1')).toBeNull();
    // The server's own tags are already the letter's words (#1766).
    expect(roundTagWords('Previous comment not addressed')).toBe('Previous comment not addressed');
    expect(roundTagWords('New')).toBe('New');
    expect(roundTagWords('Overall size')).toBe('Overall size');
  });

  it('says a set’s state in plain grey words: its newest full check, never the old pipeline’s', () => {
    const counts = { wrong: 5, right: 12, ask_architect: 0, not_compared: 30 };
    const check = { chat_id: 'c', message_id: 'm', ran_at: '2026-10-21T10:00:00Z', counts, review_result: { action: 'revise_resubmit' as const, words: 'Revise and resubmit' } };
    expect(sidebarWords({ full_check: check, drawings: 'read' })).toBe('Revise and resubmit · 5 wrong');
    expect(sidebarWords({ full_check: { ...check, counts: { ...counts, wrong: 0 }, review_result: { action: 'approved', words: 'Approved' } } })).toBe('Approved');
    expect(sidebarWords({ full_check: { ...check, review_result: { action: 'not_for_sending', words: 'Check incomplete: not for sending' } } })).toBe('Check incomplete · 5 wrong');
    expect(sidebarWords({ full_check: { ...check, review_result: null, stopped: 'cost_cap' } })).toBe('Check did not finish · 5 wrong');
    // Review needed: how many items a person decides before it can be approved.
    expect(sidebarWords({ full_check: { ...check, counts: { ...counts, wrong: 0 }, review_result: { action: 'review_needed', words: 'Review needed' } } })).toBe('Review needed · 30 to decide');
    // "Reading the drawings…" only while the set pack is really being made.
    expect(sidebarWords({ drawings: 'reading' })).toBe('Reading the drawings…');
    expect(sidebarWords({ drawings: 'read' })).toBe('Drawings read · Not checked yet');
    expect(sidebarWords({ drawings: 'failed' })).toBe('Could not read the drawings');
    expect(sidebarWords({})).toBe('Not checked yet');
    expect(sidebarWords({ checking: true, full_check: check })).toBe('Full check running…');
  });

  it('lists a full check as "Full check" with when it started, and a question by its own words', () => {
    const now = new Date(2026, 9, 19, 15, 0);
    const today = new Date(2026, 9, 19, 9, 5).toISOString();
    const earlier = new Date(2026, 9, 18, 9, 5).toISOString();
    expect(chatListWords({ title: 'Full check · Countertops and Cabinets · High effort', created_at: today, last_message_at: today }, now))
      .toEqual({ kind: 'check', label: 'Full check', when: '09:05' });
    expect(chatListWords({ title: 'Continue the full check · Cabinets', created_at: earlier, last_message_at: earlier }, now))
      .toEqual({ kind: 'check', label: 'Full check', when: '18 Oct' });
    expect(chatListWords({ title: 'Is the sink the architect’s?', created_at: today, last_message_at: today }, now))
      .toEqual({ kind: 'question', label: 'Is the sink the architect’s?', when: '' });
    expect(chatListWords({ title: null, created_at: today, last_message_at: null }, now).label).toBe('New chat');
  });

  it('never says "Claude" on any screen: the product is GV Review, a model is its short name (#1756)', () => {
    const found: string[] = [];
    for (const file of allSourceFiles()) {
      const code = withoutComments(readFileSync(file, 'utf8'));
      const match = /\bClaude\b/.exec(code);
      if (match) found.push(`${file.replace(SRC, 'src')}: "${code.slice(Math.max(0, match.index - 30), match.index + 30)}"`);
    }
    expect(found).toEqual([]);
    expect(BANNED_WORDS.some((banned) => banned.pattern.test('so Claude did not answer'))).toBe(true);
    // A model id, a URL or a lower-case package name is not reviewer text.
    expect(BANNED_WORDS.some((banned) => banned.pattern.test('claude-opus-5-5'))).toBe(false);
  });

  it('names a model by its short name everywhere (#1756)', () => {
    expect(modelLabel('claude-sonnet-5-5', [])).toBe('Sonnet 5.5');
    expect(modelLabel('anthropic.claude-opus-5-5', null)).toBe('Opus 5.5');
    expect(modelLabel('claude-opus-5-5', [{ id: 'claude-opus-5-5', label: 'Claude Opus 5.5' }])).toBe('Opus 5.5');
    expect(shortModelLabel({ label: 'Claude Opus 5.5' })).toBe('Opus 5.5');
    expect(modelName('anthropic.claude-opus-5-5')).toBe('Opus 5.5');
    expect(fallbackWords({ from_model: 'claude-fable-5-1', to_model: 'claude-opus-4-8' } as never, null))
      .toBe('Fable 5.1 declined this request, so Opus 4.8 answered it instead.');
    // No model known yet: the footer names none (it used to say "Claude").
    expect(footerWords({ model: null, effort: 'high', cost_usd: '1.72' }, null)).toBe('High effort · $1.72');
  });
});

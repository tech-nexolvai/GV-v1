/**
 * #1551: the "Understood as" line under a typed question, and its Edit: how the app's sharpen step
 * read the question (task, pages, answer length), and the reviewer's correction sent back with the
 * same question. Kept free of React so it can be tested alone.
 */

import type { ChatOutputShape, ChatTaskEdit, ChatUnderstood } from '@/lib/chat-api';

/** The answer lengths the reviewer can pick, in the screen's words. */
export const SHAPE_CHOICES: readonly { value: ChatOutputShape; words: string }[] = [
  { value: 'one_line', words: 'One-line answer' },
  { value: 'short', words: 'Short answer' },
  { value: 'findings', words: 'List of findings' },
  { value: 'file', words: 'A file' },
];

/** The line's words: the server's label, or a plain one for a question back. */
export function understoodWords(understood: ChatUnderstood): string {
  return understood.label || understood.task;
}

/** Pages as the reviewer types them ("4, 7-9"); `[]` for blank (the whole set); null when unreadable. */
export function parsePages(typed: string): number[] | null {
  const text = typed.trim();
  if (!text) return [];
  const pages: number[] = [];
  for (const part of text.split(/[,\s]+/).filter(Boolean)) {
    const range = /^(\d{1,4})(?:-(\d{1,4}))?$/.exec(part);
    if (!range) return null;
    const low = Number(range[1]);
    const high = range[2] ? Number(range[2]) : low;
    if (low < 1 || high < low || high - low > 59) return null;
    for (let page = low; page <= high; page += 1) if (!pages.includes(page)) pages.push(page);
  }
  return pages.length > 60 ? null : pages;
}

/** Pages back as the reviewer would type them: "4, 7-9". */
export function pagesText(pages: readonly number[] | null | undefined): string {
  const sorted = [...new Set(pages ?? [])].sort((a, b) => a - b);
  const parts: string[] = [];
  for (let at = 0; at < sorted.length; ) {
    let end = at;
    while (end + 1 < sorted.length && sorted[end + 1] === sorted[end] + 1) end += 1;
    parts.push(end > at ? `${sorted[at]}-${sorted[end]}` : `${sorted[at]}`);
    at = end + 1;
  }
  return parts.join(', ');
}

/** The form's starting values, from the line it edits. */
export interface UnderstoodForm {
  task: string;
  compare: string;
  pages: string;
  shape: ChatOutputShape;
}

export function formOf(understood: ChatUnderstood): UnderstoodForm {
  const asksBack = understood.intent === 'clarify' || understood.intent === 'refuse_action';
  return {
    task: asksBack ? '' : understood.task,
    compare: (understood.what_to_compare ?? []).join(', '),
    pages: understood.scope.kind === 'whole_set' ? '' : pagesText(understood.scope.pages),
    shape: asksBack ? 'short' : understood.output_shape,
  };
}

/**
 * The correction to send, or the plain reason it cannot be sent yet. The intent and checks of the
 * line are kept unless it was a question back or an action (then the server picks from the words).
 */
export function taskEditOf(understood: ChatUnderstood, form: UnderstoodForm): ChatTaskEdit | string {
  const task = form.task.trim();
  if (!task) return 'Say what to check.';
  if (task.length > 200) return 'Keep what to check under 200 characters.';
  const pages = parsePages(form.pages);
  if (pages === null) return 'Pages look like "4" or "7-9, 12" (leave it empty for the whole set).';
  const compare = form.compare
    .split(',')
    .map((item) => item.trim().slice(0, 80))
    .filter(Boolean)
    .slice(0, 6);
  const keepIntent = understood.intent !== 'clarify' && understood.intent !== 'refuse_action';
  return {
    task,
    ...(keepIntent ? { intent: understood.intent, checks_to_run: [...(understood.checks_to_run ?? [])].slice(0, 8) } : {}),
    what_to_compare: compare,
    pages,
    output_shape: form.shape,
  };
}

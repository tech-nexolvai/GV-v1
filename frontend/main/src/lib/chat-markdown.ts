/**
 * The chat's answers are free markdown (#1464): headings, lists (numbered too, with sub-points),
 * tables, code, quotes and inline bold, italic, code and links. This cuts an answer into blocks and
 * a line into inline pieces, without React, so both can be tested alone.
 *
 * Tables are read by the assistant panel's own `answerBlocks` (a "|" header with its "|---|" rule
 * under it), so the panel and the chat agree on what a table is. While an answer streams in, a
 * half-written block simply shows as words until its end arrives (an unclosed code fence shows as
 * code so far).
 */

import { answerBlocks } from '@/lib/assistant';

export interface ListItem {
  text: string;
  /** Indented "- " lines under it. */
  children: string[];
}

export type MarkdownBlock =
  | { kind: 'heading'; level: number; text: string }
  | { kind: 'paragraph'; text: string }
  | { kind: 'list'; ordered: boolean; start: number; items: ListItem[] }
  | { kind: 'table'; header: string[]; rows: string[][] }
  | { kind: 'code'; lang: string; text: string }
  | { kind: 'quote'; text: string }
  | { kind: 'rule' };

const FENCE = /^\s{0,3}(```+|~~~+)\s*([\w+#.-]*)\s*$/;
const HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const RULE = /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/;
const QUOTE = /^\s{0,3}>\s?(.*)$/;
const ITEM = /^(\s*)([-*+•]|\d{1,9}[.)])\s+(.*)$/;
const TABLE_LINE = /^\s*\|.*\|\s*$/;
const TABLE_RULE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function startsBlock(line: string, next: string | undefined): boolean {
  return FENCE.test(line) || HEADING.test(line) || RULE.test(line) || QUOTE.test(line) || ITEM.test(line)
    || (TABLE_LINE.test(line) && next !== undefined && TABLE_RULE.test(next) && next.includes('-'));
}

export function markdownBlocks(source: string): MarkdownBlock[] {
  const lines = source.replace(/\r\n?/g, '\n').split('\n');
  const blocks: MarkdownBlock[] = [];
  let at = 0;
  while (at < lines.length) {
    const line = lines[at];
    if (!line.trim()) {
      at += 1;
      continue;
    }

    const fence = FENCE.exec(line);
    if (fence) {
      const body: string[] = [];
      at += 1;
      while (at < lines.length && !(lines[at].trim().startsWith(fence[1][0].repeat(3)) && FENCE.test(lines[at]))) {
        body.push(lines[at]);
        at += 1;
      }
      at += 1; // the closing fence (or the end, while it streams in)
      blocks.push({ kind: 'code', lang: fence[2], text: body.join('\n') });
      continue;
    }

    const heading = HEADING.exec(line);
    if (heading) {
      blocks.push({ kind: 'heading', level: heading[1].length, text: heading[2] });
      at += 1;
      continue;
    }

    if (TABLE_LINE.test(line) && at + 1 < lines.length && TABLE_RULE.test(lines[at + 1]) && lines[at + 1].includes('-')) {
      const chunk = [line, lines[at + 1]];
      at += 2;
      while (at < lines.length && TABLE_LINE.test(lines[at])) {
        chunk.push(lines[at]);
        at += 1;
      }
      const table = answerBlocks(chunk.join('\n')).find((block) => block.kind === 'table');
      if (table?.kind === 'table') blocks.push({ kind: 'table', header: table.header, rows: table.rows });
      continue;
    }

    if (RULE.test(line)) {
      blocks.push({ kind: 'rule' });
      at += 1;
      continue;
    }

    if (QUOTE.test(line)) {
      const body: string[] = [];
      while (at < lines.length && QUOTE.test(lines[at])) {
        body.push(QUOTE.exec(lines[at])?.[1] ?? '');
        at += 1;
      }
      blocks.push({ kind: 'quote', text: body.join('\n') });
      continue;
    }

    const item = ITEM.exec(line);
    if (item && item[1].length < 2) {
      const ordered = /\d/.test(item[2]);
      const items: ListItem[] = [];
      while (at < lines.length) {
        const current = lines[at];
        const match = ITEM.exec(current);
        const top = match && match[1].length < 2;
        if (top && /\d/.test(match[2]) === ordered) {
          items.push({ text: match[3], children: [] });
          at += 1;
        } else if (match && !top && items.length) {
          // An indented point under the item above.
          items[items.length - 1].children.push(match[3]);
          at += 1;
        } else if (current.trim() && /^\s{2,}\S/.test(current) && items.length) {
          // An indented line carrying on the item (or its last sub-point) above.
          const last = items[items.length - 1];
          if (last.children.length) last.children[last.children.length - 1] += ` ${current.trim()}`;
          else last.text += ` ${current.trim()}`;
          at += 1;
        } else if (!current.trim()) {
          // A blank line between items keeps the list going; anything else after it ends the list.
          let ahead = at + 1;
          while (ahead < lines.length && !lines[ahead].trim()) ahead += 1;
          const next = ahead < lines.length ? ITEM.exec(lines[ahead]) : null;
          if (next && (next[1].length >= 2 || /\d/.test(next[2]) === ordered)) at = ahead;
          else break;
        } else break;
      }
      blocks.push({ kind: 'list', ordered, start: ordered ? Number.parseInt(item[2], 10) : 1, items });
      continue;
    }

    // Words up to a blank line or the start of another block.
    const body = [line];
    at += 1;
    while (at < lines.length && lines[at].trim() && !startsBlock(lines[at], lines[at + 1])) {
      body.push(lines[at]);
      at += 1;
    }
    blocks.push({ kind: 'paragraph', text: body.join('\n') });
  }
  return blocks;
}

// ── Inline ──────────────────────────────────────────────────

export type InlinePiece =
  | { kind: 'text'; text: string }
  | { kind: 'code'; text: string }
  | { kind: 'strong'; children: InlinePiece[] }
  | { kind: 'em'; children: InlinePiece[] }
  | { kind: 'strike'; children: InlinePiece[] }
  | { kind: 'link'; href: string; children: InlinePiece[] };

/** Each pattern with what it makes. The leftmost match wins; on a tie, the earlier pattern. */
const INLINE: { pattern: RegExp; make: (match: RegExpExecArray) => InlinePiece }[] = [
  { pattern: /`([^`\n]+)`/, make: (m) => ({ kind: 'code', text: m[1] }) },
  // Only web links: an answer cannot point the browser anywhere else.
  { pattern: /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/, make: (m) => ({ kind: 'link', href: m[2], children: inlinePieces(m[1]) }) },
  { pattern: /\*\*(?=\S)([^\n]*?\S)\*\*/, make: (m) => ({ kind: 'strong', children: inlinePieces(m[1]) }) },
  { pattern: /(?<![\w])__(?=\S)([^\n]*?\S)__(?![\w])/, make: (m) => ({ kind: 'strong', children: inlinePieces(m[1]) }) },
  { pattern: /~~(?=\S)([^\n]*?\S)~~/, make: (m) => ({ kind: 'strike', children: inlinePieces(m[1]) }) },
  { pattern: /(?<![*\w])\*(?=[^\s*])([^*\n]*?[^\s*])\*(?![*\w])/, make: (m) => ({ kind: 'em', children: inlinePieces(m[1]) }) },
  // Underscores only around whole words, so snake_case names stay as they are.
  { pattern: /(?<![\w])_(?=[^\s_])([^_\n]*?[^\s_])_(?![\w])/, make: (m) => ({ kind: 'em', children: inlinePieces(m[1]) }) },
];

/** One line of an answer cut into plain words, code, bold, italic, struck-through words and links. */
export function inlinePieces(text: string): InlinePiece[] {
  const pieces: InlinePiece[] = [];
  let rest = text;
  while (rest) {
    let best: { index: number; length: number; piece: InlinePiece } | null = null;
    for (const { pattern, make } of INLINE) {
      const match = pattern.exec(rest);
      if (match && (best === null || match.index < best.index)) best = { index: match.index, length: match[0].length, piece: make(match) };
    }
    if (!best) {
      pieces.push({ kind: 'text', text: rest });
      break;
    }
    if (best.index > 0) pieces.push({ kind: 'text', text: rest.slice(0, best.index) });
    pieces.push(best.piece);
    rest = rest.slice(best.index + best.length);
  }
  return pieces;
}

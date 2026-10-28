/**
 * An answer cut into running text, "- " lists and markdown tables.
 *
 * Moved here unchanged on 2026-10-28 from `lib/assistant.ts` (the old review assistant panel), because
 * the chat's markdown (`lib/chat-markdown.ts`) reads its tables with it.
 */

/** One block of an answer: running text, a "- " list, or a markdown table ("| a | b |" lines with a "|---|" line). */
export type AnswerBlock = { kind: 'text'; text: string } | { kind: 'table'; header: string[]; rows: string[][] } | { kind: 'list'; items: string[] };

const LIST_LINE = /^\s*[-•*]\s+/;

const TABLE_LINE = /^\s*\|.*\|\s*$/;
const RULE_LINE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function cells(line: string): string[] {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());
}

/** The answer cut into text and tables. A run of "|" lines is a table only with its "|---|" rule under the header. */
export function answerBlocks(text: string): AnswerBlock[] {
  const lines = text.split('\n');
  const blocks: AnswerBlock[] = [];
  let buffer: string[] = [];
  const flush = () => {
    if (buffer.length) blocks.push({ kind: 'text', text: buffer.join('\n') });
    buffer = [];
  };
  for (let at = 0; at < lines.length; ) {
    if (TABLE_LINE.test(lines[at]) && at + 1 < lines.length && RULE_LINE.test(lines[at + 1]) && lines[at + 1].includes('-')) {
      flush();
      const header = cells(lines[at]);
      const rows: string[][] = [];
      at += 2;
      while (at < lines.length && TABLE_LINE.test(lines[at])) {
        const row = cells(lines[at]);
        rows.push(header.map((_, index) => row[index] ?? ''));
        at += 1;
      }
      blocks.push({ kind: 'table', header, rows });
      continue;
    }
    if (LIST_LINE.test(lines[at])) {
      flush();
      const items: string[] = [];
      while (at < lines.length && LIST_LINE.test(lines[at])) {
        items.push(lines[at].replace(LIST_LINE, ''));
        at += 1;
      }
      blocks.push({ kind: 'list', items });
      continue;
    }
    buffer.push(lines[at]);
    at += 1;
  }
  flush();
  return blocks.filter((block) => block.kind !== 'text' || block.text.trim());
}

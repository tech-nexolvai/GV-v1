import type { ReactNode } from 'react';

type Block =
  | { kind: 'paragraph'; lines: string[] }
  | { kind: 'table'; header: string[]; rows: string[][] };

export function ChatMarkdown({ text }: { text: string }): ReactNode {
  return (
    <>
      {parseBlocks(text).map((block, index) => {
        if (block.kind === 'table') return <MarkdownTable key={index} block={block} />;
        return <MarkdownParagraph key={index} lines={block.lines} />;
      })}
    </>
  );
}

function MarkdownParagraph({ lines }: { lines: string[] }) {
  return (
    <p>
      {lines.map((line, index) => (
        <span key={index}>
          {index > 0 && <br />}
          {renderInline(line)}
        </span>
      ))}
    </p>
  );
}

function MarkdownTable({ block }: { block: Extract<Block, { kind: 'table' }> }) {
  return (
    <div className="chat-table-wrap">
      <table className="chat-table">
        <thead>
          <tr>
            {block.header.map((cell, index) => (
              <th key={index} scope="col">{renderInline(cell)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {block.rows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {block.header.map((_, cellIndex) => (
                <td key={cellIndex}>{renderInline(row[cellIndex] ?? '')}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function parseBlocks(text: string): Block[] {
  const lines = text.replace(/\r\n/g, '\n').split('\n');
  const blocks: Block[] = [];
  let index = 0;

  while (index < lines.length) {
    if (!lines[index].trim()) {
      index += 1;
      continue;
    }

    const table = parseTable(lines, index);
    if (table !== null) {
      blocks.push(table.block);
      index = table.next;
      continue;
    }

    const paragraph: string[] = [];
    while (index < lines.length && lines[index].trim()) {
      if (parseTable(lines, index) !== null) break;
      paragraph.push(lines[index]);
      index += 1;
    }
    blocks.push({ kind: 'paragraph', lines: paragraph });
  }

  return blocks;
}

function parseTable(lines: string[], start: number): { block: Block; next: number } | null {
  if (start + 1 >= lines.length) return null;
  if (!isPipeRow(lines[start]) || !isSeparatorRow(lines[start + 1])) return null;

  const header = splitPipeRow(lines[start]);
  const separator = splitPipeRow(lines[start + 1]);
  if (header.length < 2 || separator.length !== header.length) return null;

  const rows: string[][] = [];
  let index = start + 2;
  while (index < lines.length && isPipeRow(lines[index])) {
    const row = splitPipeRow(lines[index]);
    if (row.length !== header.length) break;
    rows.push(row);
    index += 1;
  }

  if (rows.length === 0) return null;
  return { block: { kind: 'table', header, rows }, next: index };
}

function isPipeRow(line: string): boolean {
  const trimmed = line.trim();
  return trimmed.includes('|') && splitPipeRow(trimmed).length >= 2;
}

function isSeparatorRow(line: string): boolean {
  const cells = splitPipeRow(line);
  return cells.length >= 2 && cells.every((cell) => /^:?-{3,}:?$/.test(cell.trim()));
}

function splitPipeRow(line: string): string[] {
  const trimmed = line.trim();
  const content = trimmed.startsWith('|') && trimmed.endsWith('|')
    ? trimmed.slice(1, -1)
    : trimmed;
  return content.split('|').map((cell) => cell.trim());
}

function renderInline(text: string): ReactNode {
  const parts = text.split(/(`[^`]+`|\*\*[^*]+\*\*)/g);
  return parts.map((part, index) => {
    if (part.startsWith('`') && part.endsWith('`')) {
      return <code key={index}>{part.slice(1, -1)}</code>;
    }
    if (part.startsWith('**') && part.endsWith('**')) {
      return <strong key={index}>{part.slice(2, -2)}</strong>;
    }
    return part;
  });
}

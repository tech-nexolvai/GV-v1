import { Fragment } from 'react';

import { CopyButton } from '@/components/chat/chat-motion';
import { cn } from '@/lib/utils';
import { inlinePieces, markdownBlocks, type InlinePiece, type MarkdownBlock } from '@/lib/chat-markdown';

/**
 * An answer's markdown (#1464, #1470), in the app's type: Geist for words, Plex Mono for code.
 * Tables are clean (thin borders, a muted header row, sideways scroll on a narrow screen); code
 * blocks carry a Copy button. While words stream in (`streaming`), each new block fades in, as
 * claude.ai's do, instead of a caret blinking after them.
 */

function Inline({ pieces }: { pieces: readonly InlinePiece[] }) {
  return (
    <>
      {pieces.map((piece, index) => {
        switch (piece.kind) {
          case 'text':
            return <Fragment key={index}>{piece.text}</Fragment>;
          case 'code':
            return <code key={index} className="rounded-[5px] bg-muted px-1 py-px font-mono text-[0.88em] break-words">{piece.text}</code>;
          case 'strong':
            return <strong key={index} className="font-semibold"><Inline pieces={piece.children} /></strong>;
          case 'em':
            return <em key={index}><Inline pieces={piece.children} /></em>;
          case 'strike':
            return <s key={index}><Inline pieces={piece.children} /></s>;
          case 'link':
            return (
              <a key={index} href={piece.href} target="_blank" rel="noopener noreferrer" className="font-medium underline decoration-muted-foreground/60 underline-offset-[3px] hover:decoration-foreground">
                <Inline pieces={piece.children} />
              </a>
            );
          default:
            return null;
        }
      })}
    </>
  );
}

function InlineText({ text }: { text: string }) {
  return <Inline pieces={inlinePieces(text)} />;
}

// Modest headings, as claude.ai's: a little larger than the words, never shouting.
const HEADING_CLASS: Record<number, string> = {
  1: 'text-[1.25em] font-semibold',
  2: 'text-[1.125em] font-semibold',
  3: 'text-[1em] font-semibold',
};

function Block({ block }: { block: MarkdownBlock }) {
  switch (block.kind) {
    case 'heading': {
      // Inside a message the page's own headings come first: an answer's "#" starts at h3.
      const Tag = (`h${Math.min(6, block.level + 2)}`) as 'h3' | 'h4' | 'h5' | 'h6';
      return (
        <Tag className={cn('mt-1.5 leading-snug tracking-[-0.005em] text-foreground', HEADING_CLASS[block.level] ?? 'text-[1em] font-medium')}>
          <InlineText text={block.text} />
        </Tag>
      );
    }
    case 'paragraph':
      return (
        <p className="break-words whitespace-pre-line">
          <InlineText text={block.text} />
        </p>
      );
    case 'list': {
      const List = block.ordered ? 'ol' : 'ul';
      return (
        <List
          start={block.ordered && block.start !== 1 ? block.start : undefined}
          className={cn('space-y-1.5 pl-6 marker:text-muted-foreground', block.ordered ? 'list-decimal' : 'list-disc')}
        >
          {block.items.map((item, index) => (
            <li key={index} className="break-words">
              <InlineText text={item.text} />
              {item.children.length > 0 && (
                <ul className="mt-1 list-[circle] space-y-1 pl-5 marker:text-muted-foreground">
                  {item.children.map((child, at) => (
                    <li key={at}><InlineText text={child} /></li>
                  ))}
                </ul>
              )}
            </li>
          ))}
        </List>
      );
    }
    case 'table':
      return (
        <div data-slot="chat-table" className="max-w-full overflow-x-auto rounded-lg border">
          <table className="w-full border-collapse text-[0.875em] leading-normal">
            <thead className="bg-muted/50">
              <tr>
                {block.header.map((cell, index) => (
                  <th key={index} scope="col" className="border-b px-4 py-2.5 text-left font-semibold whitespace-nowrap text-foreground">
                    <InlineText text={cell} />
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, at) => (
                <tr key={at} className="border-b last:border-b-0">
                  {block.header.map((_, index) => (
                    <td key={index} className="px-4 py-2.5 align-top">
                      <InlineText text={row[index] ?? ''} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    case 'code':
      return (
        <div data-slot="chat-code" className="min-w-0 overflow-hidden rounded-lg border bg-muted/40">
          <div className="flex items-center justify-between gap-2 border-b py-0.5 pr-1 pl-3">
            <span className="font-mono text-xs text-muted-foreground">{block.lang || 'code'}</span>
            <CopyButton text={block.text} label="Copy code" withWords />
          </div>
          <pre className="overflow-x-auto px-3 py-2.5 font-mono text-[12.5px] leading-relaxed">
            <code>{block.text}</code>
          </pre>
        </div>
      );
    case 'quote':
      return (
        <blockquote className="border-l-2 border-border pl-3 text-muted-foreground whitespace-pre-line">
          <InlineText text={block.text} />
        </blockquote>
      );
    case 'rule':
      return <hr className="border-border" />;
    default:
      return null;
  }
}

export function ChatMarkdown({ text, streaming = false, className }: { text: string; streaming?: boolean; className?: string }) {
  const blocks = markdownBlocks(text);
  return (
    // claude.ai's reading type: 16px words at a 1.65 line height (a caller may make it smaller).
    <div data-slot="chat-markdown" data-streaming={streaming ? 'true' : undefined} className={cn('grid min-w-0 grid-cols-[minmax(0,1fr)] gap-4 text-base leading-[1.65] text-foreground', className)}>
      {blocks.map((block, index) => (
        // A block that appears while the words stream in fades in; one already there stays still.
        <div key={`${index}-${block.kind}`} className={cn('min-w-0', streaming && 'chat-fade-in')}>
          <Block block={block} />
        </div>
      ))}
    </div>
  );
}

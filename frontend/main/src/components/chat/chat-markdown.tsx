import { Fragment } from 'react';

import { AnswerTable } from '@/components/assistant/assistant-answer';
import { cn } from '@/lib/utils';
import { inlinePieces, markdownBlocks, type InlinePiece, type MarkdownBlock } from '@/lib/chat-markdown';

/**
 * An answer's markdown (#1464), in the app's type: Geist for words, Plex Mono for code. Tables use
 * the assistant panel's own table. `streaming` puts a caret after the last words while they arrive.
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

function Caret() {
  return <span aria-hidden="true" data-slot="chat-caret" className="ml-0.5 inline-block h-[1em] w-[0.45em] animate-pulse bg-foreground align-[-0.15em] motion-reduce:animate-none" />;
}

const HEADING_CLASS: Record<number, string> = {
  1: 'text-xl font-semibold',
  2: 'text-lg font-semibold',
  3: 'text-base font-semibold',
};

function Block({ block, caret }: { block: MarkdownBlock; caret: boolean }) {
  switch (block.kind) {
    case 'heading': {
      // Inside a message the page's own headings come first: an answer's "#" starts at h3.
      const Tag = (`h${Math.min(6, block.level + 2)}`) as 'h3' | 'h4' | 'h5' | 'h6';
      return (
        <Tag className={cn('mt-2 leading-snug text-foreground', HEADING_CLASS[block.level] ?? 'text-[15px] font-semibold')}>
          <InlineText text={block.text} />
          {caret && <Caret />}
        </Tag>
      );
    }
    case 'paragraph':
      return (
        <p className="leading-relaxed break-words whitespace-pre-line">
          <InlineText text={block.text} />
          {caret && <Caret />}
        </p>
      );
    case 'list': {
      const List = block.ordered ? 'ol' : 'ul';
      return (
        <List
          start={block.ordered && block.start !== 1 ? block.start : undefined}
          className={cn('ml-1 space-y-1 pl-5 leading-relaxed marker:text-muted-foreground', block.ordered ? 'list-decimal' : 'list-disc')}
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
              {caret && index === block.items.length - 1 && <Caret />}
            </li>
          ))}
        </List>
      );
    }
    case 'table':
      return <AnswerTable header={block.header} rows={block.rows} renderCell={(cell) => <InlineText text={cell} />} />;
    case 'code':
      return (
        <div className="overflow-hidden rounded-lg border bg-muted/50">
          {block.lang && <div className="border-b px-3 py-1 font-mono text-xs text-muted-foreground">{block.lang}</div>}
          <pre className="overflow-x-auto px-3 py-2 font-mono text-[12.5px] leading-relaxed">
            <code>{block.text}</code>
            {caret && <Caret />}
          </pre>
        </div>
      );
    case 'quote':
      return (
        <blockquote className="border-l-2 border-border pl-3 text-muted-foreground whitespace-pre-line">
          <InlineText text={block.text} />
          {caret && <Caret />}
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
    <div data-slot="chat-markdown" className={cn('grid min-w-0 grid-cols-[minmax(0,1fr)] gap-3', className)}>
      {blocks.map((block, index) => (
        <Block key={index} block={block} caret={streaming && index === blocks.length - 1} />
      ))}
      {streaming && blocks.length === 0 && <span><Caret /></span>}
    </div>
  );
}

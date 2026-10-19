import { useEffect, useState } from 'react';
import { Check, Copy } from 'lucide-react';

import { useReducedMotion } from '@/lib/chat-motion';
import { cn } from '@/lib/utils';
import './chat-motion.css';

/**
 * The small moving parts of a chat message (#1470), in claude.ai's manner: GV's working sign, the
 * shimmering "Thinking…" words, a fold that opens by growing its height, a spinner and a Copy
 * button. Each one stands still when the reviewer asks the system for reduced motion.
 */

/** What the GV sign is doing: thinking, running tools, writing words, finished, or simply shown. */
export type SparkState = 'thinking' | 'working' | 'streaming' | 'done' | 'still';

/**
 * GV's AI sign (#1534): the four-point star, in GV's maroon, moving the way claude.ai's spark does.
 * Thinking: it turns slowly and breathes (about 1.6 s). Running tools or helpers: it keeps turning.
 * Writing words: it sits at the end of the text and pulses gently. Done: it settles still with a
 * short ease-out. Transforms only (no layout work); still under reduced motion.
 */
export function GVSpark({ state = 'still', size = 20, className, label }: { state?: SparkState; size?: number; className?: string; label?: string }) {
  const reduced = useReducedMotion();
  return (
    <span
      data-slot="gv-spark"
      data-state={state}
      data-motion={reduced ? 'reduce' : 'full'}
      role={label ? 'img' : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      className={cn('gv-spark inline-grid shrink-0 place-items-center text-gv-maroon', className)}
      style={{ width: size, height: size }}
    >
      <span className="gv-spark__turn grid size-full place-items-center">
        <svg viewBox="0 0 24 24" className="gv-spark__breathe size-full" fill="currentColor">
          <path d="M12 1.5c.55 5.6 4.9 9.95 10.5 10.5-5.6.55-9.95 4.9-10.5 10.5C11.45 16.9 7.1 12.55 1.5 12 7.1 11.45 11.45 7.1 12 1.5Z" />
        </svg>
      </span>
    </span>
  );
}

/** The working sign at the foot of an answer while it runs (kept as `Spark` for its callers). */
export function Spark({ label, className, state = 'thinking' }: { label: string; className?: string; state?: SparkState }) {
  return (
    <span data-slot="chat-working" data-state={state} className={cn('inline-flex items-center gap-2 text-foreground', className)}>
      <GVSpark state={state} size={20} />
      <span className="sr-only">{label}</span>
    </span>
  );
}

/** Words with a light sweeping across them while `active` (and the system allows motion). */
export function Shimmer({ active, children, className, tone = 'brand' }: { active: boolean; children: React.ReactNode; className?: string; tone?: 'brand' | 'ink' }) {
  const reduced = useReducedMotion();
  const moving = active && !reduced;
  return (
    <span data-slot="chat-shimmer" data-active={moving ? 'true' : 'false'} data-tone={tone} className={cn(moving && 'chat-shimmer', moving && tone === 'ink' && 'chat-shimmer-ink', className)}>
      {children}
    </span>
  );
}

/** A small turning ring; a still ring under reduced motion. */
export function Spinner({ className }: { className?: string }) {
  const reduced = useReducedMotion();
  return (
    <span
      aria-hidden="true"
      data-slot="chat-spinner"
      className={cn('inline-block size-3.5 shrink-0 rounded-full border-[1.5px] border-border border-t-foreground', !reduced && 'animate-spin', className)}
    />
  );
}

/**
 * A fold that opens by growing its height (about 200 ms, eased), the `grid-template-rows` way, so
 * nothing below it jumps. Its content is made only once it is first opened, and while folded it
 * cannot be reached by keyboard or screen reader. Instant under reduced motion.
 */
export function Collapse({ open, id, children, className }: { open: boolean; id?: string; children: React.ReactNode; className?: string }) {
  const reduced = useReducedMotion();
  const [opened, setOpened] = useState(open);
  if (open && !opened) setOpened(true);
  return (
    <div
      id={id}
      data-slot="chat-collapse"
      data-open={open ? 'true' : 'false'}
      inert={!open}
      aria-hidden={!open}
      className={cn(
        'grid min-w-0',
        open ? 'grid-rows-[1fr] opacity-100' : 'grid-rows-[0fr] opacity-0',
        !reduced && 'transition-[grid-template-rows,opacity] duration-200 ease-out',
        className,
      )}
    >
      <div className="min-h-0 min-w-0 overflow-hidden">{opened && children}</div>
    </div>
  );
}

/** Copy to the clipboard; says "Copied" for two seconds after. */
export function CopyButton({ text, label = 'Copy', withWords = false, className }: { text: string; label?: string; withWords?: boolean; className?: string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 2000);
    return () => window.clearTimeout(timer);
  }, [copied]);
  const words = copied ? 'Copied' : label;
  return (
    <button
      type="button"
      data-slot="chat-copy"
      onClick={() => void navigator.clipboard?.writeText(text).then(() => setCopied(true), () => setCopied(false))}
      title={words}
      aria-label={words}
      className={cn(
        'inline-flex h-7 shrink-0 items-center gap-1.5 rounded-md px-1.5 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none',
        className,
      )}
    >
      {copied ? <Check className="size-3.5" aria-hidden="true" /> : <Copy className="size-3.5" aria-hidden="true" />}
      {withWords && <span aria-hidden="true">{copied ? 'Copied' : 'Copy'}</span>}
    </button>
  );
}

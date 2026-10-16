import { useEffect, useState } from 'react';
import { Check, Copy } from 'lucide-react';

import { useReducedMotion } from '@/lib/chat-motion';
import { cn } from '@/lib/utils';
import './chat-motion.css';

/**
 * The small moving parts of a chat message (#1470), in claude.ai's manner: the working spark, the
 * shimmering "Thinking…" words, a fold that opens by growing its height, a spinner and a Copy
 * button. Each one stands still when the reviewer asks the system for reduced motion.
 */

/** claude.ai's spark: eight rays that turn and breathe while the answer is being written. */
export function Spark({ label, className }: { label: string; className?: string }) {
  const reduced = useReducedMotion();
  const rays = [0, 45, 90, 135, 180, 225, 270, 315];
  return (
    <span data-slot="chat-working" data-motion={reduced ? 'reduce' : 'full'} className={cn('inline-flex items-center gap-2 text-foreground', className)}>
      <svg viewBox="0 0 24 24" aria-hidden="true" className={cn('size-5', !reduced && 'chat-spark')}>
        {rays.map((angle, at) => {
          const length = at % 2 === 0 ? 9 : 6.5;
          const radians = (angle * Math.PI) / 180;
          return (
            <line
              key={angle}
              x1={12 + Math.cos(radians) * 2}
              y1={12 + Math.sin(radians) * 2}
              x2={12 + Math.cos(radians) * length}
              y2={12 + Math.sin(radians) * length}
              stroke="currentColor"
              strokeWidth={2.2}
              strokeLinecap="round"
            />
          );
        })}
      </svg>
      <span className="sr-only">{label}</span>
    </span>
  );
}

/** Words with a light sweeping across them while `active` (and the system allows motion). */
export function Shimmer({ active, children, className }: { active: boolean; children: React.ReactNode; className?: string }) {
  const reduced = useReducedMotion();
  const moving = active && !reduced;
  return (
    <span data-slot="chat-shimmer" data-active={moving ? 'true' : 'false'} className={cn(moving && 'chat-shimmer', className)}>
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

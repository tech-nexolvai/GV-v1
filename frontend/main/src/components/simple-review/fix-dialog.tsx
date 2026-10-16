import { useId, useRef, useState } from 'react';
import { Loader2, PenLine } from 'lucide-react';

import type { ReviewItem } from '@/api/review-items';
import { fixChoices, fixValue, metricTyped } from '@/lib/review-items';
import { Button } from '@/components/ui/button';
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import type { SaveError } from './failures-strip';
import { PageTag } from './words';

/** The plain promise the Fix dialog makes (#1368): what to type, and what it becomes. */
export const FIX_WORDS = "Type what the drawing shows, in inches. Recorded as a correction of the AI's reading.";

/**
 * Fix on an AI number (Anant, 2026-10-16): the reviewer types the value the drawing shows, in
 * inches, and it is recorded as a correction of the AI's reading (W2). The AIs' other readings are
 * offered as a quick fill; nothing is sent until "Save the correction".
 */
export function FixDialog({
  item,
  open,
  onOpenChange,
  busy,
  error,
  onSave,
}: {
  item: ReviewItem;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  busy: boolean;
  error: SaveError | null;
  onSave: (value: string) => void;
}) {
  const [typed, setTyped] = useState('');
  const id = useId();
  const box = useRef<HTMLInputElement>(null);
  const choices = fixChoices(item);
  const value = fixValue(typed);
  // Millimetres are refused by the server: never preview them as if they would be recorded (QA 4).
  const metric = metricTyped(typed);
  const same = item.best_guess !== null && item.best_guess !== undefined && value.replace(/\s/g, '') === item.best_guess.text.replace(/\s/g, '');

  function close(next: boolean) {
    if (busy) return;
    if (!next) setTyped('');
    onOpenChange(next);
  }

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent data-slot="fix-dialog" className="font-sans sm:max-w-md">
        <form
          className="flex flex-col gap-5"
          onSubmit={(event) => {
            event.preventDefault();
            if (value && !same && !metric && !busy) onSave(value);
          }}
        >
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 text-[20px] leading-7">
              <PenLine aria-hidden="true" className="size-4 text-muted-foreground" />
              Fix this number
            </DialogTitle>
            <DialogDescription className="text-[15px] leading-6">{FIX_WORDS}</DialogDescription>
          </DialogHeader>

          <div className="flex flex-wrap items-baseline gap-x-6 gap-y-2 rounded-lg border border-border bg-muted/40 px-4 py-3">
            <span className="flex min-w-0 flex-1 flex-col gap-0.5">
              <span className="truncate text-[13px] text-muted-foreground">{item.what}</span>
              <PageTag page={item.page} />
            </span>
            {item.best_guess && (
              <span className="flex flex-col items-end gap-0.5">
                <span className="text-[13px] text-muted-foreground">The AI read</span>
                <span className="num text-[20px] leading-7 font-semibold text-foreground">{item.best_guess.text}</span>
              </span>
            )}
          </div>

          <div className="flex flex-col gap-2">
            <label htmlFor={`${id}-value`} className="text-[13px] font-medium text-foreground">
              What the drawing shows
            </label>
            <div className="relative">
              <Input
                ref={box}
                id={`${id}-value`}
                autoFocus
                inputMode="decimal"
                autoComplete="off"
                spellCheck={false}
                placeholder={'e.g. 96 3/4'}
                value={typed}
                onChange={(event) => setTyped(event.target.value)}
                aria-describedby={`${id}-hint`}
                className="num h-11 pr-10 text-[20px] md:text-[20px]"
              />
              <span aria-hidden="true" className="num pointer-events-none absolute inset-y-0 right-3 flex items-center text-[15px] text-muted-foreground">in</span>
            </div>
            <p id={`${id}-hint`} role="status" className="min-h-5 text-[13px] leading-5 text-muted-foreground">
              {metric ? (
                'Inches only, e.g. 1". Millimetres are not recorded.'
              ) : same ? (
                'That is what the AI read. Close this and use Confirm instead.'
              ) : value ? (
                <>
                  Recorded as <span className="num text-foreground">{value}</span>
                </>
              ) : (
                'Whole inches, a fraction (3/4) or a decimal. Feet and inches as 8\' 1/2".'
              )}
            </p>
            {choices.length > 0 && (
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[13px] text-muted-foreground">Another AI read</span>
                {choices.map((choice) => (
                  <Button key={choice.text} type="button" size="xs" variant="outline" className="num" onClick={() => {
                      setTyped(choice.text.replace(/"$/, ''));
                      box.current?.focus();
                    }}>
                    {choice.text}
                  </Button>
                ))}
              </div>
            )}
          </div>

          {error && (
            <p role="alert" className="rounded-lg border border-outcome-fail-fg/40 bg-outcome-fail-bg px-3 py-2 text-[13px] leading-5 text-outcome-fail-fg">
              Not saved: {error.message}
            </p>
          )}

          <DialogFooter className="gap-2">
            <DialogClose asChild>
              <Button type="button" variant="ghost" disabled={busy}>
                Cancel
              </Button>
            </DialogClose>
            <Button type="submit" disabled={busy || !value || same || metric}>
              {busy && <Loader2 aria-hidden="true" className="animate-spin motion-reduce:animate-none" />}
              Save the correction
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

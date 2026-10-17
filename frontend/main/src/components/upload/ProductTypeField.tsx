/**
 * "What is this drawing set for?" — the product the reviewer picks at upload (#994).
 *
 * The choices come from the API (`GET /products`, the product registry, #1547), never from a list in
 * this file. Every product is listed; one that is not active yet is greyed "coming later", and one the
 * published rulebook has no check for is greyed too, so the dropdown cannot start a set nothing would
 * check. Only the chosen product's checks run, and the readers are told it.
 *
 * Presentational on purpose: the form owns loading and the chosen value, so this renders the same
 * on the server as in the browser and its test needs no network.
 */

import { useId } from 'react';
import { ChevronDown } from 'lucide-react';

import { canUploadFor, type ProductChoice, type ProductType } from '@/api/uploadState';
import { Label } from '@/components/ui/label';
import { cn } from '@/lib/utils';

export type ProductChoicesState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; choices: readonly ProductChoice[] };

interface ProductTypeFieldProps {
  state: ProductChoicesState;
  value: ProductType | null;
  onChange: (value: ProductType) => void;
  disabled?: boolean;
}

/**
 * A native select in the shadcn input's clothes (#1125): it keeps the phone's own picker and renders
 * its options on the server, which a Radix Select does not.
 */
export function ProductTypeField({ state, value, onChange, disabled = false }: ProductTypeFieldProps) {
  const id = useId();
  const choices = state.status === 'ready' ? state.choices : [];
  const usable = choices.filter(canUploadFor);
  const unusable = disabled || state.status !== 'ready' || usable.length === 0;
  return (
    <div className="flex flex-col gap-2">
      <Label htmlFor={id}>What is this drawing set for?</Label>
      <div className="relative w-full sm:w-64">
        <select
          id={id}
          aria-describedby={state.status === 'loading' ? undefined : `${id}-note`}
          className={cn(
            'h-9 w-full appearance-none rounded-md border border-input bg-transparent py-1 pr-9 pl-3 text-base shadow-xs outline-none md:text-sm dark:bg-input/30',
            'focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50',
            unusable && 'cursor-not-allowed opacity-50',
          )}
          value={value ?? ''}
          disabled={unusable}
          onChange={(event) => {
            const chosen = usable.find((choice) => choice.id === event.target.value);
            if (chosen) onChange(chosen.id);
          }}
          required
        >
          {state.status === 'loading' && <option value="">Loading the products…</option>}
          {state.status === 'ready' && usable.length === 0 && (
            <option value="">No checks are published yet</option>
          )}
          {choices.map((choice) => (
            <option key={choice.id} value={choice.id} disabled={!canUploadFor(choice)} title={choice.description}>
              {`${choice.label}${choice.active ? (choice.published_checks > 0 ? '' : ' (no checks published yet)') : ' (coming later)'}`}
            </option>
          ))}
        </select>
        <ChevronDown className="pointer-events-none absolute top-1/2 right-3 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
      </div>
      {state.status === 'error' && (
        <p id={`${id}-note`} className="text-xs text-destructive" role="alert">
          The list of products could not be loaded: {state.message}
        </p>
      )}
      {state.status === 'ready' && usable.length === 0 && (
        <p id={`${id}-note`} className="text-xs text-muted-foreground">
          No checks are published yet, so a drawing set could not be checked. Publish the rulebook
          first.
        </p>
      )}
      {state.status === 'ready' && usable.length > 0 && (
        <p id={`${id}-note`} className="text-xs text-muted-foreground">
          Only the checks for this product are run.
        </p>
      )}
    </div>
  );
}

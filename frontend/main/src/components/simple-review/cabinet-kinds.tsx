import { useState } from 'react';
import { Check, Loader2, Lock, MoveHorizontal } from 'lucide-react';

import type { ReviewItem } from '@/api/review-items';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { SettingLinks } from './not-checked-group';

type Group = 'keeps' | 'resized';

const GROUPS: readonly { group: Group; word: string; icon: typeof Lock }[] = [
  { group: 'keeps', word: 'Keeps its width', icon: Lock },
  { group: 'resized', word: 'Can be resized', icon: MoveHorizontal },
];

/**
 * A cabinet run's marks (#648): each cabinet a row, marked "Keeps its width" (an equipment cabinet:
 * sink, microwave, fridge, other) or "Can be resized" (single door, double door, drawer), then saved
 * once. Nothing is pre-marked by code or AI; a row shows only the reviewer's own earlier mark. The
 * per-type width limits the check lacked are links to their Company settings fields, never guessed.
 */
export function CabinetKinds({
  item,
  busy,
  onSave,
}: {
  item: ReviewItem;
  /** True while the marks are being saved. */
  busy: boolean;
  onSave: (kinds: string[]) => void;
}) {
  const cabinets = item.cabinets ?? [];
  const choices = item.cabinet_choices ?? [];
  const open = item.status === 'open' && item.actions.includes('mark_cabinets');
  const [kinds, setKinds] = useState<(string | null)[]>(() => cabinets.map((cabinet) => cabinet.marked ?? null));
  const [groups, setGroups] = useState<(Group | null)[]>(() =>
    cabinets.map((cabinet) => (cabinet.keeps_width == null ? null : cabinet.keeps_width ? 'keeps' : 'resized')),
  );
  const complete = kinds.length === cabinets.length && kinds.every((kind) => kind !== null);
  const premarked = cabinets.some((cabinet) => cabinet.marked);

  function pickGroup(index: number, group: Group) {
    setGroups((prior) => prior.map((value, at) => (at === index ? group : value)));
    // A kind from the other group no longer fits: the reviewer says which kind it is.
    setKinds((prior) =>
      prior.map((kind, at) => {
        if (at !== index || kind === null) return kind;
        const keeps = choices.find((choice) => choice.category === kind)?.keeps_width;
        return keeps === (group === 'keeps') ? kind : null;
      }),
    );
  }

  return (
    <fieldset data-slot="cabinet-kinds" className="flex min-w-0 flex-col gap-3">
      <legend className="mb-1 text-[15px] leading-6 font-medium text-foreground">{item.what}</legend>
      <p className="max-w-[68ch] text-[13px] leading-5 text-muted-foreground">
        A cabinet that keeps its width holds an appliance (sink, microwave, fridge): the check never resizes it. The others share what the fillers cannot absorb, equally.
        {premarked && ' Your earlier marks are filled in.'}
      </p>
      <ul className="grid gap-2">
        {cabinets.map((cabinet, index) => {
          const group = groups[index];
          const options = choices.filter((choice) => choice.keeps_width === (group === 'keeps'));
          return (
            <li
              key={cabinet.position}
              data-slot="cabinet-row"
              className="flex min-w-0 flex-col gap-2 rounded-xl border border-border bg-background px-4 py-3 sm:flex-row sm:flex-wrap sm:items-center sm:gap-x-4"
            >
              <span className="flex min-w-0 flex-1 flex-col">
                <span className="text-[15px] leading-6 font-medium text-foreground">
                  Cabinet <span className="num">{cabinet.position}</span>
                </span>
                <span className="text-[13px] leading-5 text-muted-foreground">
                  Architect <span className="num text-foreground">{cabinet.architect_width ?? '—'}</span>
                  {' · '}
                  Vendor <span className="num text-foreground">{cabinet.vendor_width ?? '—'}</span>
                </span>
              </span>
              <span role="group" aria-label={`Cabinet ${cabinet.position}`} className="flex flex-wrap items-center gap-2">
                {GROUPS.map(({ group: value, word, icon: Icon }) => (
                  <button
                    key={value}
                    type="button"
                    aria-pressed={group === value}
                    disabled={!open || busy}
                    onClick={() => pickGroup(index, value)}
                    className={cn(
                      'inline-flex h-8 items-center gap-1.5 rounded-lg border px-3 text-[13px] font-medium outline-none',
                      'focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-60',
                      group === value
                        ? 'border-foreground/55 bg-muted text-foreground'
                        : 'border-border text-muted-foreground hover:border-foreground/30 hover:text-foreground',
                    )}
                  >
                    <Icon aria-hidden="true" className="size-3.5" />
                    {word}
                  </button>
                ))}
                {group && (
                  <select
                    aria-label={`Cabinet ${cabinet.position}: which kind`}
                    value={kinds[index] ?? ''}
                    disabled={!open || busy}
                    onChange={(event) => {
                      const chosen = event.target.value || null;
                      setKinds((prior) => prior.map((kind, at) => (at === index ? chosen : kind)));
                    }}
                    className="h-8 rounded-lg border border-border bg-background px-2 text-[13px] text-foreground outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:opacity-60"
                  >
                    <option value="">Which kind?</option>
                    {options.map((choice) => (
                      <option key={choice.category} value={choice.category}>
                        {choice.label}
                      </option>
                    ))}
                  </select>
                )}
              </span>
            </li>
          );
        })}
      </ul>
      {(item.settings ?? []).length > 0 && (
        <p data-slot="cabinet-limits" className="flex flex-wrap gap-x-1 text-[13px] leading-5 text-muted-foreground">
          <span className="font-medium text-foreground">Not checked: set in Company settings.</span>
          <span>The cabinet width limits have no company value; each project sets its own.</span>
          <SettingLinks settings={item.settings ?? []} />
        </p>
      )}
      {open && (
        <div className="flex flex-wrap items-center gap-2">
          <Button onClick={() => complete && onSave(kinds as string[])} disabled={!complete || busy}>
            {busy ? <Loader2 aria-hidden="true" className="animate-spin motion-reduce:animate-none" /> : <Check aria-hidden="true" />}
            Save marks
          </Button>
          {!complete && <span className="text-[13px] text-muted-foreground">Mark every cabinet to save.</span>}
        </div>
      )}
    </fieldset>
  );
}

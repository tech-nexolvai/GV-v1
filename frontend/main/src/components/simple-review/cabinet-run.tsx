import { useState } from 'react';
import { Check, Loader2 } from 'lucide-react';

import type { CabinetRunAnswer, ReviewItem } from '@/api/review-items';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { SettingLinks } from './not-checked-group';

/** The answer that names a piece a filler, beside the cabinet kinds the check offers. */
const FILLER = 'filler';

/**
 * A cabinet run proposed from the AI's stored widths (cabinets e2e). Each width was read alike by
 * two AIs and checked by code; nothing is typed but the site wall-to-wall, which is not on any
 * drawing (the client's sheet: "F_Wall_2_Wall_Dim · User Input"). The person says what each piece
 * is: a filler, or which kind of cabinet (a sink, microwave or fridge cabinet keeps its width; a
 * door or drawer cabinet can be resized). Only a piece the vendor printed "Filler" on comes
 * pre-selected: never from its width or its place. Rows that were read but could not be proposed
 * are listed with the reason, so nothing read vanishes.
 */
export function CabinetRun({
  item,
  busy,
  onUse,
}: {
  item: ReviewItem;
  /** True while the answer is being saved. */
  busy: boolean;
  onUse: (answer: CabinetRunAnswer) => void;
}) {
  const proposal = item.cabinet_proposal;
  const runs = proposal?.runs ?? [];
  const choices = item.cabinet_choices ?? [];
  const open = item.status === 'open' && item.actions.includes('use_cabinet_run');
  const [chosen, setChosen] = useState<string | null>(runs.length === 1 ? runs[0].row_id : null);
  const run = runs.find((candidate) => candidate.row_id === chosen) ?? null;
  const [kinds, setKinds] = useState<Record<string, (string | null)[]>>(() =>
    Object.fromEntries(runs.map((candidate) => [candidate.row_id, candidate.pieces.map((piece) => piece.proposed ?? null)])),
  );
  const savedSite = proposal?.site_width?.display ?? null;
  const notProposed = proposal?.not_proposed ?? [];
  const [site, setSite] = useState('');
  const marks = run ? (kinds[run.row_id] ?? []) : [];
  const complete = run !== null && marks.length === run.pieces.length && marks.every((mark) => mark !== null);
  const shapeOk = complete && marks[0] === FILLER && marks[marks.length - 1] === FILLER && marks.slice(1, -1).every((mark) => mark !== FILLER) && marks.length >= 3;
  const siteOk = site.trim() !== '' || savedSite !== null;
  const ready = open && complete && shapeOk && siteOk;

  function mark(index: number, value: string | null) {
    if (!run) return;
    setKinds((prior) => ({
      ...prior,
      [run.row_id]: (prior[run.row_id] ?? []).map((kind, at) => (at === index ? value : kind)),
    }));
  }

  return (
    <fieldset data-slot="cabinet-run" className="flex min-w-0 flex-col gap-4">
      <legend className="mb-1 text-[15px] leading-6 font-medium text-foreground">{item.what}</legend>
      {runs.length === 0 ? (
        <p data-slot="cabinet-run-none" className="max-w-[68ch] text-[13px] leading-5 text-muted-foreground">
          {proposal?.note ?? 'No cabinet run was read. Enter the run on Measurements.'}
        </p>
      ) : (
        <>
          <p className="max-w-[68ch] text-[13px] leading-5 text-muted-foreground">
            The AIs read these widths on the vendor's drawing; each was read the same way by two AIs and checked by code. Say what each piece is. A cabinet that keeps its width holds an appliance (sink, microwave, fridge): the check never resizes it.
          </p>
          {runs.length > 1 && (
            <div role="radiogroup" aria-label="Which run to check" className="flex flex-wrap gap-2">
              {runs.map((candidate) => (
                <button
                  key={candidate.row_id}
                  type="button"
                  role="radio"
                  aria-checked={chosen === candidate.row_id}
                  disabled={!open || busy}
                  onClick={() => setChosen(candidate.row_id)}
                  className={cn(
                    'inline-flex h-8 items-center gap-1.5 rounded-lg border px-3 text-[13px] font-medium outline-none',
                    'focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-60',
                    chosen === candidate.row_id ? 'border-foreground/55 bg-muted text-foreground' : 'border-border text-muted-foreground hover:text-foreground',
                  )}
                >
                  Page <span className="num">{candidate.page}</span> · <span className="num">{candidate.total.display}</span>
                </button>
              ))}
            </div>
          )}
          {run && (
            <>
              <ol className="grid gap-2" aria-label={`Run on page ${run.page}, left to right`}>
                {run.pieces.map((piece, index) => (
                  <li
                    key={piece.candidate_id}
                    data-slot="cabinet-run-piece"
                    className="flex min-w-0 flex-col gap-2 rounded-xl border border-border bg-background px-4 py-3 sm:flex-row sm:items-center sm:gap-x-4"
                  >
                    <span className="flex min-w-0 flex-1 flex-col">
                      <span className="text-[15px] leading-6 font-medium text-foreground">
                        Piece <span className="num">{piece.position}</span> · <span className="num">{piece.width.display}</span>
                      </span>
                      <span className="text-[13px] leading-5 text-muted-foreground">
                        Printed <span className="num text-foreground">{piece.printed}</span>
                        {piece.proposed === FILLER && ' · the vendor wrote "Filler"'}
                      </span>
                    </span>
                    <select
                      aria-label={`Piece ${piece.position}: what it is`}
                      value={marks[index] ?? ''}
                      disabled={!open || busy}
                      onChange={(event) => mark(index, event.target.value || null)}
                      className="h-8 rounded-lg border border-border bg-background px-2 text-[13px] text-foreground outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:opacity-60"
                    >
                      <option value="">What is it?</option>
                      <option value={FILLER}>Filler</option>
                      <optgroup label="Keeps its width">
                        {choices.filter((choice) => choice.keeps_width).map((choice) => (
                          <option key={choice.category} value={choice.category}>
                            {choice.label}
                          </option>
                        ))}
                      </optgroup>
                      <optgroup label="Can be resized">
                        {choices.filter((choice) => !choice.keeps_width).map((choice) => (
                          <option key={choice.category} value={choice.category}>
                            {choice.label}
                          </option>
                        ))}
                      </optgroup>
                    </select>
                  </li>
                ))}
              </ol>
              <ul className="grid gap-1 text-[13px] leading-5 text-muted-foreground" aria-label="What code checked">
                {run.checks.map((check) => (
                  <li key={check} className="flex gap-1.5">
                    <Check aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
                    {check}
                  </li>
                ))}
                <li data-slot="cabinet-run-architect">{run.architect_note}</li>
              </ul>
            </>
          )}
          <label className="flex max-w-md flex-col gap-1.5 text-[13px] leading-5">
            <span className="font-medium text-foreground">Site wall-to-wall (measured on site)</span>
            <span className="text-muted-foreground">
              The width between the two walls on site, in inches. It is not on any drawing.
              {savedSite && (
                <>
                  {' '}Saved: <span className="num text-foreground">{savedSite}</span>. Leave empty to keep it.
                </>
              )}
            </span>
            <Input
              inputMode="decimal"
              placeholder={'e.g. 96 1/2"'}
              value={site}
              disabled={!open || busy}
              onChange={(event) => setSite(event.target.value)}
              aria-label="Site wall-to-wall in inches"
              className="num"
            />
          </label>
        </>
      )}
      {notProposed.length > 0 && (
        <details data-slot="cabinet-run-not-proposed" className="text-[13px] leading-5 text-muted-foreground">
          <summary className="cursor-pointer">Rows read but not proposed ({notProposed.length})</summary>
          <ul className="mt-1 grid gap-1">
            {notProposed.map((entry) => (
              <li key={`${entry.page}:${entry.row_id ?? ''}`}>
                Page <span className="num">{entry.page}</span>: {entry.why}
              </li>
            ))}
          </ul>
        </details>
      )}
      {(item.settings ?? []).length > 0 && (
        <p data-slot="cabinet-limits" className="flex flex-wrap gap-x-1 text-[13px] leading-5 text-muted-foreground">
          <span className="font-medium text-foreground">Not checked: set in Company settings.</span>
          <span>The cabinet width limits have no company value; each project sets its own.</span>
          <SettingLinks settings={item.settings ?? []} />
        </p>
      )}
      {open && run && (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            onClick={() =>
              ready &&
              onUse({ row_id: run.row_id, pieces: marks as string[], site_width: site.trim() === '' ? null : site.trim() })
            }
            disabled={!ready || busy}
          >
            {busy ? <Loader2 aria-hidden="true" className="animate-spin motion-reduce:animate-none" /> : <Check aria-hidden="true" />}
            Use this run
          </Button>
          {!complete ? (
            <span className="text-[13px] text-muted-foreground">Say what every piece is.</span>
          ) : !shapeOk ? (
            <span className="text-[13px] text-muted-foreground">The check needs one filler at each end and cabinets between; otherwise check this run by hand.</span>
          ) : !siteOk ? (
            <span className="text-[13px] text-muted-foreground">Enter the site wall-to-wall.</span>
          ) : null}
        </div>
      )}
    </fieldset>
  );
}

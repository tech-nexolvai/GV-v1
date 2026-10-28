/**
 * "Needs: …" for a check waiting on a value (#1181), in place, in the queue item and in Other checks.
 *
 * One line per value the server says the check could not run without: what it is, its unit, and
 * where it is found. A setting for this job gets a box; the sink's size opens the job's sink entry; a
 * cabinet list, a cabinet's type or the site width says it is entered on Measurements (#1205); a
 * number drawn on the vendor's drawing gets no box at all — people confirm readings, they do not fill
 * in measurements (GV's rule, Anant 2026-10-12) — and its line says where it is drawn. One
 * source note for the settings, one Save, through the same values endpoint Measurements uses. Nothing
 * is pre-filled. A saved value is shown with where it is from, and the check keeps saying it is
 * waiting until the checks run again — so the line under it says exactly that, with the review's own
 * "Run checks".
 *
 * A depth or sink check the depth and sink reader read (#1295) also says, per countertop, what was
 * read and how it was checked, a reading to confirm ("Confirm this reading", the existing confirm
 * path), or why a value was not used. Without readings the card is exactly as it was.
 *
 * With the depth and sink reader switched on, a depth or sink check also offers "Read depths again"
 * (#1308), for a set read before the reader existed or was switched on; it asks first, as it costs money.
 */
import { useId, useState } from 'react';

import { enterMeasurements } from '@/api/client';
import { projectId } from '@/api/config';
import type { Finding } from '@/data/types';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { SIGNED_OFF_NOTE, useSignedOff } from '@/lib/signed-off';
import { SinkEntryDialog } from '@/components/settings/SinkEntry';
import { DepthReadings } from '@/components/results/depth-readings';
import { ReadDepthsAgain } from '@/components/results/read-depths-again';
import {
  overridable,
  saveState,
  savedFrom,
  savedSinceResult,
  sinkNeeded,
  typeable,
  type MissingInput,
} from '@/lib/waiting-values';

const SELECT_CLASS =
  'h-8 rounded-md border border-input bg-transparent px-2 text-sm outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50';

export function NeedsValues({
  finding,
  packageId,
  onSaved,
  onRunChecks,
}: {
  finding: Finding;
  packageId: string;
  /** A value was recorded: the page reloads the results and asks for a run. */
  onSaved: () => void;
  /** The review's existing "Run checks" action. */
  onRunChecks?: () => void;
}) {
  const id = useId();
  const inputs = finding.missing_inputs ?? [];
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [chosen, setChosen] = useState<Record<string, string>>({});
  const [note, setNote] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedHere, setSavedHere] = useState(false);
  const [sinkOpen, setSinkOpen] = useState(false);
  const signedOff = useSignedOff();
  // What the depth and sink reader read for each countertop (#1295): only when it read the set.
  const readings = finding.depth_readings ?? [];
  if (inputs.length === 0 && readings.length === 0) return null;
  // A closed review takes no new values (#1182): say so instead of offering boxes that would be refused.
  if (signedOff) {
    const note = (
      <p className="text-sm text-muted-foreground" role="note" data-part="signed-off">
        {SIGNED_OFF_NOTE}
      </p>
    );
    if (readings.length === 0) return note;
    return (
      <section data-slot="needs-values" aria-label="Values this check needs" className="flex flex-col gap-3 rounded-lg border border-dashed p-3 font-sans text-sm">
        <DepthReadings readings={readings} packageId={packageId} canAct={false} onConfirmed={onSaved} />
        {note}
      </section>
    );
  }

  const toType = typeable(inputs);
  const sink = sinkNeeded(inputs);
  const { entry, blocked } = saveState(inputs, drafts, note, chosen);
  const runFirst = savedHere || savedSinceResult(inputs);

  async function save() {
    if (!entry || saving) return;
    setSaving(true);
    setError(null);
    try {
      await enterMeasurements(projectId(), packageId, entry);
      setDrafts({});
      setNote('');
      setSavedHere(true);
      onSaved();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section data-slot="needs-values" aria-label="Values this check needs" className="flex flex-col gap-3 rounded-lg border border-dashed p-3 font-sans text-sm">
      {readings.length > 0 && (
        <DepthReadings readings={readings} packageId={packageId} canAct onConfirmed={onSaved} onRunChecks={onRunChecks} />
      )}
      {/* Read the depths again on a set already read (#1308): only when the reader is switched on. */}
      <ReadDepthsAgain packageId={packageId} checkId={finding.check_id} />
      {inputs.length > 0 && (
        <ul className="flex flex-col gap-2.5">
          {inputs.map((input) => (
            <NeedsLine
              key={input.key}
              id={`${id}-${input.key}`}
              input={input}
              draft={drafts[input.key] ?? ''}
              chosen={chosen[input.key] ?? ''}
              disabled={saving}
              onDraft={(value) => setDrafts((prior) => ({ ...prior, [input.key]: value }))}
              onChoose={(value) => setChosen((prior) => ({ ...prior, [input.key]: value }))}
            />
          ))}
        </ul>
      )}
      {sink.length > 0 && (
        <div>
          <Button type="button" size="sm" variant="outline" onClick={() => setSinkOpen(true)}>
            Enter the sink…
          </Button>
        </div>
      )}
      {toType.length > 0 && (
        <form
          className="flex flex-col gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            void save();
          }}
        >
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`${id}-note`}>
              Source note <span className="font-normal text-muted-foreground">(optional)</span>
            </Label>
            <Input id={`${id}-note`} value={note} maxLength={200} disabled={saving} onChange={(event) => setNote(event.target.value)} />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button type="submit" size="sm" disabled={!entry || saving} aria-describedby={blocked ? `${id}-why` : undefined}>
              {saving ? 'Saving…' : 'Save'}
            </Button>
            {blocked && (
              <span id={`${id}-why`} className="text-xs text-muted-foreground" data-slot="needs-save-why">
                {blocked}
              </span>
            )}
          </div>
        </form>
      )}
      {error && <p role="alert" className="text-outcome-fail-fg">{error}</p>}
      {runFirst && (
        <div data-slot="needs-run-checks" className="flex flex-wrap items-center gap-2" role="status">
          <span>Saved. Run checks to update this result.</span>
          {onRunChecks && (
            <Button type="button" size="sm" variant="outline" onClick={onRunChecks}>
              Run checks
            </Button>
          )}
        </div>
      )}
      <SinkEntryDialog
        open={sinkOpen}
        packageId={packageId}
        onOpenChange={setSinkOpen}
        onSaved={() => {
          setSinkOpen(false);
          setSavedHere(true);
          onSaved();
        }}
      />
    </section>
  );
}

function NeedsLine({
  id,
  input,
  draft,
  chosen,
  disabled,
  onDraft,
  onChoose,
}: {
  id: string;
  input: MissingInput;
  draft: string;
  chosen: string;
  disabled: boolean;
  onDraft: (value: string) => void;
  onChoose: (value: string) => void;
}) {
  const sources = input.sources ?? [];
  const boxed = input.askable && input.scope === 'project' && (input.saved == null || overridable(input));

  // The reviewer's own entry on Measurements (#1205): a list of cabinets, each cabinet's type, the
  // site width. No box here: it is entered there, where a run keeps its order. Never "the drawing".
  if (input.scope === 'measurements') {
    return (
      <li className="flex flex-col gap-0.5" data-scope={input.scope} data-key={input.key} data-askable="false">
        {input.saved != null ? (
          <p>
            <span className="font-medium">{input.label}:</span> <span className="num">{input.saved.text}</span>{' '}
            <span className="text-muted-foreground">({savedFrom(input)})</span>
          </p>
        ) : (
          <p>
            <span className="font-medium">Needs:</span> {input.label}
            {input.unit && <span className="num text-muted-foreground"> ({input.unit})</span>}
            <span className="text-muted-foreground"> — {input.where_to_find}</span>
          </p>
        )}
      </li>
    );
  }

  // A drawn number: said plainly, no box. The check waits for a reading of it to be confirmed.
  if (!input.askable) {
    return (
      <li className="flex flex-col gap-0.5" data-scope={input.scope} data-key={input.key} data-askable="false">
        {input.saved != null ? (
          <p>
            <span className="font-medium">{input.label}:</span> <span className="num">{input.saved.text}</span>{' '}
            <span className="text-muted-foreground">({savedFrom(input)})</span>
          </p>
        ) : (
          <>
            {/* Where it is drawn, in the Measure page's own words (#1262). Nothing here knows whether a
                drawing was supplied, so the line never says it was not. */}
            <p>
              <span className="font-medium">Needs:</span> {input.label}
              {input.unit && <span className="num text-muted-foreground"> ({input.unit})</span>}
              <span className="text-muted-foreground"> — {input.where_to_find}</span>
            </p>
            <p className="text-xs text-muted-foreground">
              A number drawn on the drawing is confirmed from a reading on Measurements, never typed here. Until one is
              confirmed, this check waits for it.
            </p>
          </>
        )}
      </li>
    );
  }

  return (
    <li className="flex flex-col gap-1.5" data-scope={input.scope} data-key={input.key}>
      {input.saved != null && input.scope === 'sink' ? (
        <p>
          <span className="font-medium">In use for this package:</span> {input.label} <span className="num">{input.saved.text}</span>{' '}
          <span className="text-muted-foreground">({savedFrom(input)})</span>
        </p>
      ) : (
        <p>
          <span className="font-medium">Needs:</span> {boxed ? <label htmlFor={id}>{input.label}</label> : input.label}
          {input.unit && <span className="num text-muted-foreground"> ({input.unit})</span>}
          <span className="text-muted-foreground"> — {input.where_to_find}</span>
        </p>
      )}
      {input.saved != null && input.scope !== 'sink' && (
        <p className="text-xs text-muted-foreground" data-slot="needs-saved">
          Saved: <span className="num text-foreground">{input.saved.text}</span> · {savedFrom(input)} · {input.saved.set_by}
        </p>
      )}
      {boxed && (
        <div className="flex flex-wrap items-center gap-2">
          <Input
            id={id}
            className="num h-8 w-32"
            placeholder={input.unit ?? undefined}
            aria-label={overridable(input) ? `${input.label} for this job, over GV's default` : undefined}
            value={draft}
            disabled={disabled}
            onChange={(event) => onDraft(event.target.value)}
          />
          {overridable(input) && <span className="text-xs text-muted-foreground">Optional: a value for this job wins over GV&apos;s default.</span>}
          {sources.length > 1 && (
            <select
              className={SELECT_CLASS}
              aria-label={`Where ${input.label.toLowerCase()} came from`}
              value={chosen}
              disabled={disabled}
              onChange={(event) => onChoose(event.target.value)}
            >
              <option value="">Where it came from…</option>
              {sources.map((source) => (
                <option key={source} value={source}>
                  {source}
                </option>
              ))}
            </select>
          )}
        </div>
      )}
    </li>
  );
}

/**
 * The project's sink (#1181): entered once, read by every sink check of every package in the job.
 *
 * The sink's inside size is on its spec sheet, never on the shop drawing, so the reviewer types it:
 * its name, inside width and inside depth (Anant, 2026-10-12: only those; the front offset stays GV's
 * standard or a project setting). The boxes start empty — nothing is suggested or pre-filled. What
 * this package's checks use is shown above them: the sink card's size, or a size typed for this
 * package on Measurements, which wins there. Shown as a small card on Measurements, and in a dialog
 * from a sink check's "Needs" line.
 */
import { useId, useState } from 'react';

import { ApiError, getProjectSink, getSinkInUse, saveProjectSink, type ProjectSink, type SinkInUse } from '@/api/client';
import { projectId } from '@/api/config';
import { useAsync } from '@/api/useAsync';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { SIGNED_OFF_NOTE, useSignedOff } from '@/lib/signed-off';

type Sink = NonNullable<ProjectSink['sink']>;

const ORIGIN_WORDS = { sink_card: 'from the sink card', package: 'typed for this package' } as const;

/** The sink card's entry in one line: name, then the sizes in the number font. */
export function SinkSummary({ sink }: { sink: Sink }) {
  return (
    <p className="text-sm" data-slot="sink-summary">
      <span className="font-medium">{sink.model_name}</span>
      {' · inside '}
      <span className="num">{sink.inside_width?.text ?? '—'}</span>
      {' wide × '}
      <span className="num">{sink.inside_depth?.text ?? '—'}</span>
      {' deep'}
      <span className="block text-xs text-muted-foreground">
        Entered by {sink.set_by}
        {sink.source_note && <> · {sink.source_note}</>}
      </span>
    </p>
  );
}

/** "In use for this package: …", each size with where it is from. Nothing in use says so. */
export function SinkInUseLine({ inUse }: { inUse: SinkInUse }) {
  const sizes = [
    ['wide', inUse.inside_width],
    ['deep', inUse.inside_depth],
  ] as const;
  if (!inUse.inside_width && !inUse.inside_depth) {
    return <p className="text-sm" data-slot="sink-in-use">In use for this package: no sink size yet. The sink checks wait.</p>;
  }
  return (
    <p className="text-sm" data-slot="sink-in-use">
      <span className="font-medium">In use for this package:</span> {inUse.model_name && <>{inUse.model_name}, </>}
      {sizes.map(([word, size], index) => (
        <span key={word}>
          {index > 0 && ' × '}
          {size ? (
            <>
              <span className="num">{size.text}</span> {word} <span className="text-muted-foreground">({ORIGIN_WORDS[size.origin]})</span>
            </>
          ) : (
            <>not set {word}</>
          )}
        </span>
      ))}
    </p>
  );
}

/** The boxes and Save. Empty on every opening: a sink is typed from its spec sheet, not edited from memory. */
export function SinkEntryForm({ onSaved, onCancel }: { onSaved: (saved: ProjectSink) => void; onCancel?: () => void }) {
  const id = useId();
  const [name, setName] = useState('');
  const [width, setWidth] = useState('');
  const [depth, setDepth] = useState('');
  const [note, setNote] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const missing = [!name.trim() && 'the sink name', !width.trim() && 'the inside width', !depth.trim() && 'the inside depth'].filter(Boolean);
  const ready = missing.length === 0;

  async function save() {
    if (!ready || saving) return;
    setSaving(true);
    setError(null);
    try {
      const saved = await saveProjectSink(projectId(), {
        model_name: name.trim(),
        inside_width: width.trim(),
        inside_depth: depth.trim(),
        ...(note.trim() ? { source_note: note.trim() } : {}),
      });
      onSaved(saved);
    } catch (caught) {
      setError(
        caught instanceof ApiError && caught.status === 404
          ? 'Only a reviewer or an admin can enter the sink.'
          : caught instanceof Error
            ? caught.message
            : String(caught),
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <form
      data-slot="sink-entry-form"
      className="flex flex-col gap-3 font-sans"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <p role="note" className="rounded-md border border-dashed px-3 py-2 text-sm">
        This sink applies to every package in this job. A size typed for one package on Measurements still wins there.
      </p>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${id}-name`}>Sink name or model</Label>
        <Input id={`${id}-name`} value={name} maxLength={80} disabled={saving} onChange={(e) => setName(e.target.value)} />
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${id}-width`}>Inside width <span className="num font-normal text-muted-foreground">(in)</span></Label>
          <Input id={`${id}-width`} className="num" placeholder="in" value={width} disabled={saving} onChange={(e) => setWidth(e.target.value)} />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${id}-depth`}>Inside depth <span className="num font-normal text-muted-foreground">(in)</span></Label>
          <Input id={`${id}-depth`} className="num" placeholder="in" value={depth} disabled={saving} onChange={(e) => setDepth(e.target.value)} />
        </div>
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${id}-note`}>Source note <span className="font-normal text-muted-foreground">(optional)</span></Label>
        <Input id={`${id}-note`} value={note} maxLength={100} disabled={saving} onChange={(e) => setNote(e.target.value)} />
      </div>
      <p className="text-xs text-muted-foreground">Type each size with its unit, from the sink&apos;s spec sheet.</p>
      {error && <p role="alert" className="text-sm text-outcome-fail-fg">{error}</p>}
      <div className="flex flex-wrap items-center gap-2">
        <Button type="submit" size="sm" disabled={!ready || saving}>{saving ? 'Saving…' : 'Save the sink'}</Button>
        {onCancel && <Button type="button" size="sm" variant="ghost" onClick={onCancel} disabled={saving}>Cancel</Button>}
        {!ready && <span className="text-xs text-muted-foreground" data-slot="sink-save-why">Type {missing.join(', ')} to save.</span>}
      </div>
    </form>
  );
}

/** The small card on Measurements: what this package uses, the sink card's entry, and the boxes. */
export function SinkEntryCard({ packageId, onSaved }: { packageId?: string | null; onSaved?: () => void }) {
  const [version, setVersion] = useState(0);
  const [editing, setEditing] = useState(false);
  const [justSaved, setJustSaved] = useState(false);
  const signedOff = useSignedOff();
  const current = useAsync(() => getProjectSink(projectId()), [version]);
  const inUse = useAsync(() => (packageId ? getSinkInUse(projectId(), packageId) : Promise.resolve(null)), [packageId, version]);
  const sink = current.status === 'ready' ? current.data.sink : null;

  return (
    <section data-slot="sink-entry" aria-labelledby="sink-entry-title" className="flex flex-col gap-3 rounded-xl border bg-card p-4 font-sans">
      <div className="flex flex-col gap-1">
        <h3 id="sink-entry-title" className="text-base font-semibold">Sink</h3>
        <p className="text-sm text-muted-foreground">Entered once for the job, from the sink&apos;s spec sheet. Every sink check of every package uses it.</p>
      </div>
      {inUse.status === 'ready' && inUse.data && <SinkInUseLine inUse={inUse.data} />}
      {current.status === 'loading' && <p className="text-sm text-muted-foreground" role="status">Loading the sink…</p>}
      {current.status === 'error' && <p role="alert" className="text-sm">The sink could not be loaded: {current.error.message}</p>}
      {current.status === 'ready' && !editing && (
        <>
          {sink ? (
            <div>
              <p className="mb-0.5 text-xs text-muted-foreground">Sink card</p>
              <SinkSummary sink={sink} />
            </div>
          ) : (
            <p className="text-sm">No sink entered for this job yet.</p>
          )}
          {justSaved && <p role="status" className="text-sm">Saved. Run checks to update the sink results.</p>}
          {signedOff ? (
            // A closed review changes nothing (#1182); the sink is changed from an open review.
            <p className="text-sm text-muted-foreground" role="note" data-part="signed-off">{SIGNED_OFF_NOTE}</p>
          ) : (
            <div>
              <Button size="sm" variant="outline" onClick={() => { setJustSaved(false); setEditing(true); }}>
                {sink ? 'Change the sink' : 'Enter the sink'}
              </Button>
            </div>
          )}
        </>
      )}
      {editing && (
        <SinkEntryForm
          onCancel={() => setEditing(false)}
          onSaved={() => {
            setEditing(false);
            setJustSaved(true);
            setVersion((n) => n + 1);
            onSaved?.();
          }}
        />
      )}
    </section>
  );
}

/** The same entry, opened from a sink check's "Needs" line. */
export function SinkEntryDialog({
  open,
  packageId,
  onOpenChange,
  onSaved,
}: {
  open: boolean;
  packageId: string;
  onOpenChange: (open: boolean) => void;
  onSaved: () => void;
}) {
  const inUse = useAsync(() => (open ? getSinkInUse(projectId(), packageId) : Promise.resolve(null)), [open, packageId]);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>The job&apos;s sink</DialogTitle>
          <DialogDescription>Entered once from the sink&apos;s spec sheet; every sink check uses it once the checks run again.</DialogDescription>
        </DialogHeader>
        {inUse.status === 'ready' && inUse.data && <SinkInUseLine inUse={inUse.data} />}
        {open && <SinkEntryForm onCancel={() => onOpenChange(false)} onSaved={() => onSaved()} />}
      </DialogContent>
    </Dialog>
  );
}

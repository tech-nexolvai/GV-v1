/**
 * The project's sink (#1181): entered once, read by every sink check of every package in the job.
 *
 * The sink's inside size is on its spec sheet, never on the shop drawing, so the reviewer types it:
 * its name, inside width and inside depth (Anant, 2026-10-12: only those; the front offset stays GV's
 * standard or a project setting). The boxes start empty — nothing is suggested or pre-filled. What
 * this package's checks use is shown above them: the sink card's size, or a size typed for this
 * package on Measurements, which wins there. Shown as a small card on Measurements, and in a dialog
 * from a sink check's "Needs" line.
 *
 * Where the depth and sink reader read the set, the vendor's own printed note naming the sink is shown
 * as a hint (#1295). It is never filled into a box: the size is typed from the spec sheet.
 */
import { useId, useState } from 'react';

import { ApiError, saveProjectSink, type ProjectSink } from '@/api/client';
import { projectId } from '@/api/config';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';

type Sink = NonNullable<ProjectSink['sink']>;

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

/** The boxes and Save. Empty on every opening: a sink is typed from its spec sheet, not edited from memory. */
export function SinkEntryForm({
  onSaved,
  onCancel,
  forProject,
}: {
  onSaved: (saved: ProjectSink) => void;
  onCancel?: () => void;
  /** The project to save for; the configured one when left out (Company settings → Projects names it, #1341). */
  forProject?: string;
}) {
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
      const saved = await saveProjectSink(forProject ?? projectId(), {
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
        This sink applies to every package in this job. A size saved earlier for one package still wins there.
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

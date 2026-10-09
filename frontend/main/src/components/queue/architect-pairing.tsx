import { useEffect, useEffectEvent, useState } from 'react';
import { RefreshCw } from 'lucide-react';

import { ApiError, getArchitectPairing, saveArchitectPairing, type ArchitectPairing, type ArchitectSpan, type CountertopResult } from '@/api/client';
import { useAsync } from '@/api/useAsync';
import { cn } from '@/lib/utils';
import { draftBody, draftProblem, vendorSideWords, type DraftPair } from '@/lib/architect';
import { spanMark, type Mark } from '@/lib/drawing-viewer';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group';

const JUDGED_BY: Record<string, string> = {
  code: 'Only code matched these',
  'both-ais': 'Only the AIs matched these',
  'code+ais': 'Code and both AIs matched these',
  reviewer: 'A reviewer matched these',
};

/**
 * "Matches the architect?" when the pairing needs the reviewer (#1085): confirm the pairing one
 * judgment made, pair it differently, or say nothing on the architect's drawing is comparable.
 * Every choice is sent only on its own click; nothing is pre-selected. The result counts only after
 * the checks run again, as with a correction.
 */
export function ArchitectPairingPanel({
  projectId,
  packageId,
  row,
  canConfirm,
  onSaved,
  onMarks,
}: {
  projectId: string;
  packageId: string;
  row: CountertopResult;
  /** True when one judgment paired the row (code only, or both AIs only): "Confirm the pairing" applies. */
  canConfirm: boolean;
  onSaved: () => void;
  /** The offered spans as marks, and the one the reviewer is looking at. */
  onMarks: (marks: Mark[], active: string | null) => void;
}) {
  const [version, setVersion] = useState(0);
  const pairing = useAsync(() => getArchitectPairing(projectId, packageId, row.row_id), [projectId, packageId, row.row_id, version]);
  const [picking, setPicking] = useState(false);
  const [draft, setDraft] = useState<DraftPair[]>([]);
  const [note, setNote] = useState('');
  const [saving, setSaving] = useState(false);
  const [problem, setProblem] = useState<{ text: string; reload: boolean } | null>(null);

  const data: ArchitectPairing | null = pairing.status === 'ready' ? pairing.data : null;
  const spans = data?.spans ?? [];
  const byId = new Map(spans.map((span) => [span.candidate_id, span]));
  const effective = data?.effective ?? null;
  const confirmable = canConfirm && effective !== null && effective.needs_confirmation && effective.pairs.length > 0;

  // The spans go to the drawing once loaded; the one the reviewer is looking at stands out.
  const [looking, setLooking] = useState<string | null>(null);
  const report = useEffectEvent(() => onMarks(spans.map(spanMark), looking));
  useEffect(() => {
    report();
  }, [data, looking]);
  const look = (span: ArchitectSpan | null) => setLooking(span ? `span:${span.candidate_id}` : null);

  async function send(pairs: ReturnType<typeof draftBody>, requireNote: boolean) {
    if (saving) return;
    if (requireNote && !note.trim()) {
      setProblem({ text: 'Say why nothing on the architect’s drawing is comparable.', reload: false });
      return;
    }
    setSaving(true);
    setProblem(null);
    try {
      await saveArchitectPairing(projectId, packageId, row.row_id, { pairs, note: note.trim() || null });
      onSaved();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        setProblem({ text: error.message || "This row's pairing was just updated. Reload it before pairing again.", reload: true });
      } else {
        setProblem({ text: error instanceof Error ? error.message : String(error), reload: false });
      }
    } finally {
      setSaving(false);
    }
  }

  function reload() {
    setProblem(null);
    setDraft([]);
    setPicking(false);
    setVersion((n) => n + 1);
  }

  if (pairing.status === 'loading') return <p className="text-sm text-muted-foreground" role="status">Loading the pairing…</p>;
  if (pairing.status === 'error') {
    return (
      <div className="flex flex-col items-start gap-2 text-sm" role="alert">
        <p>The pairing could not be loaded: {pairing.error.message}</p>
        <Button size="sm" variant="outline" onClick={() => setVersion((n) => n + 1)}><RefreshCw /> Try again</Button>
      </div>
    );
  }

  const draftIssue = draftProblem(draft, spans);
  return (
    <section data-slot="architect-pairing" aria-label="Pair with the architect" className="flex flex-col gap-3 rounded-lg border p-3">
      {effective && effective.pairs.length > 0 && !picking && (
        <div className="flex flex-col gap-1 text-sm">
          <p className="text-xs text-muted-foreground">{JUDGED_BY[effective.source] ?? `Paired by ${effective.source}`}:</p>
          <ul className="flex flex-col gap-0.5" aria-label="The pairing">
            {effective.pairs.map((pair) => (
              <li key={pair.architect_candidate_id}>
                {vendorSideWords(pair.kind, pair.vendor_slot_indices)} ↔ the architect&apos;s{' '}
                <span className="num font-medium">{byId.get(pair.architect_candidate_id)?.printed ?? 'dimension'}</span>
              </li>
            ))}
          </ul>
          {/* The server's own reasons (for example, a pair it left out because a span is now held). */}
          {effective.reasons.length > 0 && (
            <ul className="flex list-disc flex-col gap-0.5 pl-4 text-xs text-muted-foreground" aria-label="Why">
              {effective.reasons.map((reason) => <li key={reason}>{reason}</li>)}
            </ul>
          )}
        </div>
      )}

      {picking && (
        <div className="flex flex-col gap-2" data-slot="pairing-picker">
          <p className="text-sm">
            Choose what each of the architect&apos;s dimensions measures on this row (<span className="num">{data?.piece_count ?? 0}</span> {data?.piece_count === 1 ? 'piece' : 'pieces'}): the overall, or one piece. A run of pieces is not compared yet.
          </p>
          <ul className="flex flex-col gap-2" aria-label="The architect's dimensions on this page">
            {spans.map((span) => (
              <SpanChoice
                key={span.candidate_id}
                span={span}
                pieceCount={data?.piece_count ?? 0}
                rowPage={row.page_number}
                value={draft.find((pair) => pair.candidateId === span.candidate_id) ?? null}
                onChange={(next) => setDraft((current) => [...current.filter((pair) => pair.candidateId !== span.candidate_id), ...(next ? [next] : [])])}
                onLook={look}
              />
            ))}
            {spans.length === 0 && <li className="text-sm text-muted-foreground">No architect dimension is stored on this page.</li>}
          </ul>
          {draft.length > 0 && draftIssue && <p className="text-xs text-outcome-review-fg" role="status">{draftIssue}</p>}
        </div>
      )}

      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`architect-note-${row.row_id}`}>Note <span className="font-normal text-muted-foreground">(needed for &ldquo;Nothing comparable&rdquo;)</span></Label>
        <Textarea id={`architect-note-${row.row_id}`} value={note} onChange={(event) => setNote(event.target.value)} maxLength={500} rows={2} />
      </div>

      {problem && (
        <div className="flex flex-wrap items-center gap-2 text-sm text-destructive" role="alert">
          <span>{problem.text}</span>
          {problem.reload && <Button size="sm" variant="outline" onClick={reload}><RefreshCw /> Reload the pairing</Button>}
        </div>
      )}

      <div className="flex flex-wrap gap-2">
        {picking ? (
          <>
            <Button type="button" disabled={saving || draftIssue !== null} onClick={() => void send(draftBody(draft), false)}>
              {saving ? 'Saving…' : 'Save this pairing'}
            </Button>
            <Button type="button" variant="ghost" disabled={saving} onClick={() => { setPicking(false); setDraft([]); look(null); }}>
              Back
            </Button>
          </>
        ) : (
          <>
            {confirmable && (
              <Button
                type="button"
                disabled={saving}
                onClick={() => void send(effective!.pairs.map((pair) => ({ kind: pair.kind as 'overall' | 'piece', architect_candidate_id: pair.architect_candidate_id, vendor_slot_indices: [...pair.vendor_slot_indices] })), false)}
              >
                {saving ? 'Saving…' : 'Confirm the pairing'}
              </Button>
            )}
            <Button type="button" variant="outline" disabled={saving} onClick={() => setPicking(true)}>
              {confirmable ? 'Pair it differently' : 'Pair it'}
            </Button>
            <Button type="button" variant="outline" disabled={saving} onClick={() => void send([], true)}>
              Nothing comparable
            </Button>
          </>
        )}
      </div>
      <p className="text-xs text-muted-foreground">A pairing counts once the checks run again.</p>
    </section>
  );
}

/** One architect span in the picker: what it pairs with, or why it cannot be paired. */
function SpanChoice({
  span,
  pieceCount,
  rowPage,
  value,
  onChange,
  onLook,
}: {
  span: ArchitectSpan;
  pieceCount: number;
  rowPage: number;
  value: DraftPair | null;
  onChange: (next: DraftPair | null) => void;
  onLook: (span: ArchitectSpan | null) => void;
}) {
  const kind = value?.kind ?? 'none';
  return (
    <li
      data-span={span.candidate_id}
      data-can-pair={span.can_pair}
      className={cn('flex flex-col gap-1.5 rounded-md border p-2 text-sm outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50', !span.can_pair && 'bg-muted/40 text-muted-foreground')}
      // A refused span has no controls; it can still be reached, to see it on the drawing.
      tabIndex={span.can_pair ? undefined : 0}
      onMouseEnter={() => onLook(span)}
      onMouseLeave={() => onLook(null)}
      onFocus={() => onLook(span)}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="num font-medium">{span.printed}</span>
        {span.inches && span.inches !== span.printed && <span className="num text-xs text-muted-foreground">({span.inches})</span>}
        {!span.location && <span className="text-xs text-muted-foreground">not outlined: its position is not stored</span>}
        {span.location && span.location.page_number !== rowPage && <span className="text-xs text-muted-foreground">on page <span className="num">{span.location.page_number}</span></span>}
      </div>
      {!span.can_pair ? (
        <p className="text-xs" data-part="refusal">{span.refusal ?? span.held_reason ?? 'This dimension cannot be paired.'}</p>
      ) : (
        <>
          <ToggleGroup
            type="single"
            variant="outline"
            size="sm"
            value={kind}
            aria-label={`What the architect's ${span.printed} measures`}
            onValueChange={(next) => {
              if (!next || next === 'none') onChange(null);
              else onChange({ candidateId: span.candidate_id, kind: next as 'overall' | 'piece', pieces: next === 'piece' ? (value?.pieces ?? []) : [] });
            }}
            className="flex-wrap"
          >
            <ToggleGroupItem value="none">Not this row</ToggleGroupItem>
            <ToggleGroupItem value="overall">The overall</ToggleGroupItem>
            <ToggleGroupItem value="piece">One piece</ToggleGroupItem>
          </ToggleGroup>
          {kind === 'piece' && (
            <ToggleGroup
              type="single"
              variant="outline"
              size="sm"
              value={value?.pieces[0] !== undefined ? String(value.pieces[0]) : ''}
              aria-label={`Which piece the architect's ${span.printed} measures`}
              onValueChange={(next) => onChange({ candidateId: span.candidate_id, kind: 'piece', pieces: next === '' ? [] : [Number(next)] })}
              className="flex-wrap"
            >
              {Array.from({ length: pieceCount }, (_, index) => (
                <ToggleGroupItem key={index} value={String(index)} className="num">{index + 1}</ToggleGroupItem>
              ))}
            </ToggleGroup>
          )}
        </>
      )}
    </li>
  );
}

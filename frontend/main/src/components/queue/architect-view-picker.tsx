import { useId, useRef, useState } from 'react';
import { History, ImageOff, RefreshCw } from 'lucide-react';

import {
  ApiError,
  architectViewPictureUrl,
  getArchitectViewMatch,
  pickArchitectView,
  type ArchitectMatch,
  type ArchitectViewCandidate,
  type ArchitectViewMatch,
  type CountertopResult,
} from '@/api/client';
import { useAsync } from '@/api/useAsync';
import { cn } from '@/lib/utils';
import { viewHeading } from '@/lib/architect';
import { outlineOf, secondPageOf } from '@/lib/drawing-viewer';
import { FramedPage } from '@/components/drawing/framed-page';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { Skeleton } from '@/components/ui/skeleton';
import { Textarea } from '@/components/ui/textarea';

/** The value of the "None of these" choice; a view's id is never this. */
export const NONE_OF_THESE = 'none-of-these';
const NOTE_LIMIT = 500;

/**
 * "Choose which of the architect's views shows this countertop" (#1168), when code and both AIs did
 * not agree on one view. The candidates come ranked by code, each with its picture, what the architect
 * printed for it, code's score in one line, and which AI picked it. NOTHING is pre-selected: not code's
 * pick, not the AIs' pick, not the view remembered from an earlier revision. "None of these" is always
 * offered. The choice is sent only on its own click, naming the record on screen; it counts once the
 * checks run again.
 */
export function ArchitectViewPicker({
  projectId,
  packageId,
  row,
  onSaved,
}: {
  projectId: string;
  packageId: string;
  row: Pick<CountertopResult, 'row_id' | 'architect'>;
  onSaved: () => void;
}) {
  const [version, setVersion] = useState(0);
  const answer = useAsync(() => getArchitectViewMatch(projectId, packageId, row.row_id), [projectId, packageId, row.row_id, version]);
  const [choice, setChoice] = useState('');
  const [note, setNote] = useState('');
  const [saving, setSaving] = useState(false);
  // A second click before the first answer arrives must not send the pick twice.
  const sending = useRef(false);
  const [problem, setProblem] = useState<{ text: string; reload: boolean } | null>(null);
  const noteId = useId();

  function reload() {
    setProblem(null);
    setChoice('');
    setVersion((n) => n + 1);
  }

  if (answer.status === 'loading') {
    return (
      <div className="flex flex-col gap-2" role="status" aria-label="Loading the architect's views">
        <Skeleton className="h-40" />
        <Skeleton className="h-24" />
      </div>
    );
  }
  if (answer.status === 'error') {
    return (
      <div className="flex flex-col items-start gap-2 text-sm" role="alert">
        <p>The architect&apos;s views could not be loaded: {answer.error.message}</p>
        <Button size="sm" variant="outline" onClick={() => setVersion((n) => n + 1)}><RefreshCw /> Try again</Button>
      </div>
    );
  }

  const data: ArchitectViewMatch = answer.data;
  const candidates = [...data.candidates].sort((a, b) => a.rank - b.rank);
  const chosen = candidates.find((candidate) => candidate.view.view_id === choice) ?? null;
  const none = choice === NONE_OF_THESE;
  const match: ArchitectMatch | null = row.architect?.match ?? null;

  async function save() {
    if (sending.current || !data.current || (!chosen && !none)) return;
    if (chosen && !chosen.can_pick) return;
    sending.current = true;
    setSaving(true);
    setProblem(null);
    try {
      await pickArchitectView(projectId, packageId, row.row_id, {
        view_id: none ? null : chosen!.view.view_id,
        none_of_these: none,
        note: note.trim() || null,
        // The record on screen: if someone changed it since, the server refuses (409) and records nothing.
        expected_record_id: data.current.record_id,
      });
      onSaved();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        setProblem({ text: error.message || "Someone else changed this countertop's view while you had it open. Reload to see it, then choose again.", reload: true });
      } else {
        // A refusal (422) in the server's own words; the choice stays so it can be changed.
        setProblem({ text: error instanceof Error ? error.message : String(error), reload: false });
      }
    } finally {
      sending.current = false;
      setSaving(false);
    }
  }

  const vendorRegion = outlineOf(data.vendor.region?.polygon);
  const right = chosen ? secondPageOf(chosen.view) : null;
  return (
    <section data-slot="architect-view-picker" aria-labelledby={`${noteId}-title`} className="flex flex-col gap-3 rounded-lg border p-3">
      <div className="flex flex-col gap-1">
        <h3 id={`${noteId}-title`} className="text-sm font-medium">Choose which of the architect&apos;s views shows this countertop</h3>
        <p className="text-xs text-muted-foreground" data-part="order">
          Nothing is chosen for you.{' '}
          {/* The order is the server's (#1166): code's ranking when code had a pick, else the views
              both AIs called the same first. Said as it is, never as a recommendation. */}
          {match?.code_pick_view_id
            ? 'In code’s order, by what is drawn; what the AIs said is on each view.'
            : 'Views the AIs called the same come first, then code’s order by what is drawn.'}
          {data.vendor.references.length > 0 && <> The vendor&apos;s sheet refers to <span className="num">{data.vendor.references.join(', ')}</span>.</>}
        </p>
        {/* Why the reviewer is asked, in the record's own words. */}
        {(data.current?.reasons.length ?? 0) > 0 && (
          <ul className="flex list-disc flex-col gap-0.5 pl-4 text-xs text-muted-foreground" aria-label="Why you are asked" data-part="reasons">
            {data.current!.reasons.map((reason) => <li key={reason}>{reason}</li>)}
          </ul>
        )}
      </div>

      {/* Side by side: the vendor's countertop and the view being looked at. */}
      <div data-slot="view-side-by-side" className="grid h-[26rem] grid-rows-2 overflow-hidden rounded-md border sm:h-64 sm:grid-cols-2 sm:grid-rows-1">
        <FramedPage
          slot="view-vendor-pane"
          className="border-b sm:border-r sm:border-b-0"
          projectId={projectId}
          packageId={packageId}
          page={data.vendor.page_number}
          documentVersionId={data.vendor.document_version_id}
          region={vendorRegion}
          heading={<>Vendor · page <span className="num">{data.vendor.page_number}</span>{data.vendor.title ? <> · {data.vendor.title}</> : null}</>}
          regionLabel="The vendor's countertop"
          pictureAlt={`Vendor drawing, page ${data.vendor.page_number}`}
        />
        {right ? (
          <FramedPage
            key={right.key}
            slot="view-architect-pane"
            projectId={projectId}
            packageId={packageId}
            page={right.page}
            documentVersionId={right.documentVersionId}
            region={right.region}
            heading={<>Architect · {chosen!.view.label}</>}
            regionLabel={`Architect's view: ${right.heading}`}
            pictureAlt={`Architect's drawing, page ${right.page}`}
          />
        ) : (
          <p data-slot="view-architect-pane" className="m-auto p-4 text-center text-xs text-muted-foreground">
            {none ? 'None of the views is chosen.' : 'Choose a view below to see it here, beside the vendor’s.'}
          </p>
        )}
      </div>

      <RadioGroup value={choice} onValueChange={(value) => { setChoice(value); setProblem(null); }} aria-label="The architect's views" className="gap-2">
        {candidates.map((candidate) => (
          <CandidateCard key={candidate.view.view_id} candidate={candidate} match={match} projectId={projectId} packageId={packageId} selected={choice === candidate.view.view_id} />
        ))}
        {candidates.length === 0 && <p className="text-sm text-muted-foreground">The architect&apos;s file has no views to choose from.</p>}
        {data.can_choose_none && (
          <label
            data-candidate={NONE_OF_THESE}
            className={cn('flex cursor-pointer items-start gap-3 rounded-md border p-2.5 text-sm', none && 'border-foreground ring-1 ring-foreground')}
          >
            <RadioGroupItem value={NONE_OF_THESE} aria-label="None of these" className="mt-0.5" />
            <span className="flex flex-col gap-0.5">
              <span className="font-medium">None of these</span>
              <span className="text-xs text-muted-foreground">No view in the architect&apos;s drawings shows this countertop, so nothing is compared.</span>
            </span>
          </label>
        )}
      </RadioGroup>

      <div className="flex flex-col gap-1.5">
        <Label htmlFor={noteId}>Note <span className="font-normal text-muted-foreground">(optional)</span></Label>
        <Textarea id={noteId} value={note} onChange={(event) => setNote(event.target.value)} maxLength={NOTE_LIMIT} rows={2} />
        <span className="num self-end text-xs text-muted-foreground">{note.length}/{NOTE_LIMIT}</span>
      </div>

      {problem && (
        <div className="flex flex-wrap items-center gap-2 text-sm text-destructive" role="alert">
          <span>{problem.text}</span>
          {problem.reload && <Button size="sm" variant="outline" onClick={reload}><RefreshCw /> Reload the views</Button>}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        {/* No record yet (an older run): there is nothing to answer until the checks run again. */}
        {!data.current && <span className="text-xs text-muted-foreground" role="status">This countertop has no match record yet. Run the checks first.</span>}
        <Button type="button" disabled={saving || !data.current || (!chosen && !none) || (chosen !== null && !chosen.can_pick)} onClick={() => void save()}>
          {saving ? 'Saving…' : none ? 'Save: none of these' : 'Use this view'}
        </Button>
        <span className="text-xs text-muted-foreground">Your choice counts once the checks run again.</span>
      </div>
    </section>
  );
}

/**
 * One ranked view: its picture, what the architect printed, code's score in one line, which AI picked
 * it, and "remembered from an earlier revision" when it was the choice there. A view that cannot be
 * chosen says why and has no control.
 */
function CandidateCard({
  candidate,
  match,
  projectId,
  packageId,
  selected,
}: {
  candidate: ArchitectViewCandidate;
  match: ArchitectMatch | null;
  projectId: string;
  packageId: string;
  selected: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [broken, setBroken] = useState(false);
  const id = useId();
  const ids = { radio: `${id}-radio`, rank: `${id}-rank`, name: `${id}-name`, place: `${id}-place`, score: `${id}-score`, facts: `${id}-facts`, tags: `${id}-tags`, refusal: `${id}-refusal`, evidence: `${id}-evidence` };
  const { view, code } = candidate;
  const codePick = match?.code_pick_view_id === view.view_id;
  const hasFacts = code.run_length_error_display !== null || code.bays_vendor !== null || code.bays_architect !== null;
  const hasTags = codePick || candidate.ai_picked_by.length > 0 || candidate.remembered || !candidate.shown_to_ais;
  // The radio is named by the view's heading and described by what the card says about it, so a
  // screen reader hears the card's own text, never a label that replaces it.
  const described = [ids.place, ids.score, hasFacts ? ids.facts : null, hasTags ? ids.tags : null].filter(Boolean).join(' ');
  return (
    <div
      data-candidate={view.view_id}
      data-can-pick={candidate.can_pick}
      className={cn(
        'flex items-start gap-3 rounded-md border p-2.5 text-sm',
        !candidate.can_pick && 'bg-muted/40 text-muted-foreground',
        selected && 'border-foreground ring-1 ring-foreground',
      )}
    >
      {candidate.can_pick ? (
        <RadioGroupItem id={ids.radio} value={view.view_id} aria-labelledby={`${ids.rank} ${ids.name}`} aria-describedby={described} className="mt-0.5" />
      ) : (
        <span className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      )}
      <span className="num w-5 shrink-0 text-xs text-muted-foreground" aria-hidden="true">{candidate.rank}</span>
      <div className="flex min-w-0 flex-1 flex-col gap-1.5">
      {/* Only the picture and the heading choose the view: the card's buttons stay outside the label. */}
      <label htmlFor={candidate.can_pick ? ids.radio : undefined} className={cn('flex min-w-0 flex-1 items-start gap-3', candidate.can_pick && 'cursor-pointer')}>
        <span className="flex h-20 w-28 shrink-0 items-center justify-center overflow-hidden rounded-sm border bg-white">
          {view.picture_url && !broken ? (
            <img
              src={architectViewPictureUrl(projectId, packageId, view.view_id)}
              alt={`Architect's view: ${viewHeading(view)}`}
              className="max-h-full max-w-full object-contain"
              onError={() => setBroken(true)}
            />
          ) : (
            <span className="flex flex-col items-center gap-1 p-1 text-center text-xs text-muted-foreground">
              <ImageOff className="size-4" aria-hidden="true" />
              {broken ? 'Picture could not be loaded' : 'No picture stored'}
            </span>
          )}
        </span>
        <span className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="font-medium"><span id={ids.rank} className="sr-only">{`View ${candidate.rank}:`}</span><span id={ids.name}>{viewHeading(view)}</span></span>
          <span id={ids.place} className="text-xs text-muted-foreground">
            {view.file_name}, page <span className="num">{view.page_number}</span>
            {view.scale_note && <> · scale <span className="num">{view.scale_note}</span></>}
          </span>
          <span id={ids.score} className="text-xs">{candidate.score_summary}</span>
        </span>
      </label>
      <div className="flex min-w-0 flex-col gap-1 sm:pl-[8.5rem]">
        {hasFacts && (
          <span id={ids.facts} className="flex flex-wrap gap-x-3 text-xs text-muted-foreground" data-part="code-facts">
            {code.run_length_error_display !== null && <span>Run off by <span className="num text-foreground">{code.run_length_error_display} in</span></span>}
            {(code.bays_vendor !== null || code.bays_architect !== null) && (
              <span>Bays <span className="num text-foreground">{code.bays_vendor ?? '—'}</span> vendor · <span className="num text-foreground">{code.bays_architect ?? '—'}</span> architect</span>
            )}
          </span>
        )}
        {hasTags && (
          <span id={ids.tags} className="flex flex-wrap gap-1.5 text-xs" data-part="tags">
            {codePick && <Tag>Code&apos;s pick</Tag>}
            {candidate.ai_picked_by.length > 0 && <Tag>Called the same by {candidate.ai_picked_by.join(' and ')}</Tag>}
            {candidate.remembered && <Tag><History className="size-3" aria-hidden="true" /> Remembered from an earlier revision</Tag>}
            {!candidate.shown_to_ais && <Tag muted>Not shown to the AIs</Tag>}
          </span>
        )}
        {!candidate.can_pick && <span id={ids.refusal} className="text-xs" data-part="refusal">{candidate.refusal ?? 'This view cannot be chosen.'}</span>}
        {candidate.evidence.length > 0 && (
          <>
            <button
              type="button"
              aria-expanded={open}
              aria-controls={ids.evidence}
              onClick={() => setOpen((value) => !value)}
              className="self-start text-xs text-muted-foreground underline-offset-2 hover:underline"
            >
              {open ? 'Hide why' : 'Why?'}
            </button>
            <ul id={ids.evidence} hidden={!open} className="flex list-disc flex-col gap-0.5 pl-4 text-xs text-muted-foreground" aria-label="Why it was ranked here">
              {candidate.evidence.map((line) => <li key={line}>{line}</li>)}
            </ul>
          </>
        )}
      </div>
      </div>
    </div>
  );
}

function Tag({ children, muted = false }: { children: React.ReactNode; muted?: boolean }) {
  return (
    <span className={cn('inline-flex items-center gap-1 rounded-full border px-2 py-0.5', muted ? 'border-dashed text-muted-foreground' : 'text-foreground')}>
      {children}
    </span>
  );
}

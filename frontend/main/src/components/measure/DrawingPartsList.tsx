import { useState, type ReactNode } from 'react';

import {
  KIND_LABEL,
  PART_KINDS,
  codeToSend,
  decisionLabel,
  drawingLabel,
  startingCode,
  stillToDecide,
  type PartDrawing,
  type PartKind,
  type PartPoint,
  type SuggestedPart,
} from './drawingPartChoices.js';
import { GvMarksWarning } from './GvMarksWarning.js';
import type { DecisionFeedback } from './decisionFeedback.js';
import { VendorPagePlacement } from './VendorPagePlacement.js';
import { Button } from '@/components/ui/button';
import { ChoiceMark, DecisionLine, Hint, INPUT_CLASS, SELECT_CLASS, StepSection } from './wizard-ui.js';
import { choiceVariant, countWords } from './wizardWords.js';

/** A part a person adds, as the form hands it over. */
export interface NewPart {
  kind: PartKind;
  code: string | null;
  ends: [PartPoint, PartPoint];
}

/**
 * The parts of each drawing: what the computer suggests, and a person deciding each one (#882).
 *
 * **Why a person is asked.** A check that sums cabinets needs to know which cabinets there are, and
 * the computer's suggestions are read from the drawing's dimension lines, which are often partial.
 * A suggestion counts for nothing until a person says what it is, so a wrong one cannot reach a
 * check unseen.
 *
 * **One part at a time.** Each part has its own buttons and its own code box, and there is
 * deliberately no "confirm all": each decision is a person looking at one part.
 *
 * **Each part shows its own picture (#897)**, the vendor's drawing round it, once the worker has cut
 * it; a part with a code also shows where the code is printed. A part with no picture says so, and
 * says where to find it, rather than showing some other region. The picture leads the card (#1124):
 * a person decides a part by looking at it.
 *
 * **A picture that shows GV's own coloured marks says so under it (#921)**, because the marks are
 * baked into the vendor's drawing there and the picture cannot leave them out.
 */
export function DrawingPartsList({
  drawings,
  loadPagePicture,
  saving,
  feedback,
  renderPicture,
  renderCrop,
  onConfirm,
  onWithdraw,
  onAdd,
}: {
  drawings: readonly PartDrawing[];
  loadPagePicture: (viewId: string) => Promise<Blob>;
  /** The part (or, while adding, the drawing) being saved, so its buttons cannot be pressed twice. */
  saving: string | null;
  feedback?: Readonly<Record<string, DecisionFeedback>>;
  /** The part's own picture, for a part that has one. */
  renderPicture: (drawing: PartDrawing, part: SuggestedPart) => ReactNode;
  /** The crop of the reading a part's code came from, for a part that has one. */
  renderCrop: (drawing: PartDrawing, part: SuggestedPart) => ReactNode;
  onConfirm: (part: SuggestedPart, kind: PartKind, code: string | null) => void;
  onWithdraw: (part: SuggestedPart) => void;
  onAdd: (drawing: PartDrawing, part: NewPart) => void;
}) {
  const open = stillToDecide(drawings);
  return (
    <StepSection
      id="drawing-parts-title"
      slot="drawing-parts"
      title="Parts of each drawing"
      line={
        <strong className="font-medium text-foreground">
          {open === 0 ? 'Nothing left to decide.' : `${countWords(open, 'part')} still to decide.`}
        </strong>
      }
      tipLabel="About suggested parts"
      tip={
        <>
          <p>The computer suggests the parts it finds in each vendor&apos;s drawing. A suggestion counts for nothing until you say what it is.</p>
          <p>Decide each one on its own: say whether it is a cabinet, a filler or a countertop (a filler is drawn like a cabinet, so it is suggested as one), correct its code if the drawing prints it differently, or say it is not a part.</p>
        </>
      }
    >
      {drawings.map((drawing) => (
        <article className="flex flex-col gap-3" key={drawing.view_id}>
          <h4 className="text-sm font-medium">
            Page {drawing.page_index + 1}: {drawingLabel(drawing)}
          </h4>
          {drawing.why_not && <Hint>{drawing.why_not}</Hint>}
          {drawing.parts.length === 0 ? (
            <Hint>Nothing was suggested on this drawing.</Hint>
          ) : (
            <ol className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {drawing.parts.map((part) => (
                <PartRow
                  key={part.proposal_id}
                  drawing={drawing}
                  part={part}
                  saving={saving === part.proposal_id}
                  feedback={feedback?.[part.proposal_id]}
                  picture={part.has_picture ? renderPicture(drawing, part) : null}
                  crop={part.has_crop ? renderCrop(drawing, part) : null}
                  onConfirm={onConfirm}
                  onWithdraw={onWithdraw}
                />
              ))}
            </ol>
          )}
          {drawing.can_confirm && (
            <AddPartForm
              loadPagePicture={loadPagePicture}
              drawing={drawing}
              saving={saving === drawing.view_id || feedback?.[drawing.view_id]?.kind === 'saving'}
              feedback={feedback?.[drawing.view_id]}
              onAdd={onAdd}
            />
          )}
        </article>
      ))}
    </StepSection>
  );
}

function PartRow({
  drawing,
  part,
  saving,
  feedback,
  picture,
  crop,
  onConfirm,
  onWithdraw,
}: {
  drawing: PartDrawing;
  part: SuggestedPart;
  saving: boolean;
  feedback?: DecisionFeedback;
  picture: ReactNode;
  crop: ReactNode;
  onConfirm: (part: SuggestedPart, kind: PartKind, code: string | null) => void;
  onWithdraw: (part: SuggestedPart) => void;
}) {
  const [code, setCode] = useState(() => startingCode(part));
  const page = drawing.page_index + 1;
  const confirmedKind = part.decision?.decision === 'confirmed' ? part.decision.kind : null;
  const pending = saving || feedback?.kind === 'saving';
  const codeId = `part-code-${part.proposal_id}`;
  return (
    <li
      data-slot="drawing-part"
      data-decided={part.decision !== null}
      className="flex min-w-0 flex-col gap-3 rounded-xl border bg-card p-3 data-[decided=true]:bg-muted/40"
    >
      <div className="flex flex-col gap-2">
        {picture ?? (
          <Hint className="rounded-lg border border-dashed p-3">
            No picture of this part is stored yet. Find it on page {page}: it is number {part.position} from the left in this drawing.
          </Hint>
        )}
        {picture && <GvMarksWarning marks={part.picture_gv_marks} />}
        {crop && (
          <figure className="flex flex-col gap-1">
            {crop}
            <figcaption className="text-xs text-muted-foreground">Where its code is printed</figcaption>
          </figure>
        )}
      </div>
      <div className="flex flex-col gap-0.5">
        <p className="text-sm font-medium">
          {part.position}. Suggested as a {KIND_LABEL[part.suggested_kind].toLowerCase()}
          {part.added_by_a_person ? ' (added by a person)' : ''}
        </p>
        <Hint>{part.reason}</Hint>
        <Hint>{part.suggested_code ? `Code read on the drawing: “${part.suggested_code}”` : 'No code read.'}</Hint>
        <p className="text-xs font-medium">{decisionLabel(part)}</p>
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor={codeId} className="text-xs text-muted-foreground">
          Code as printed (leave empty if none)
        </label>
        <input
          id={codeId}
          type="text"
          className={`${INPUT_CLASS} num`}
          value={code}
          maxLength={200}
          disabled={!drawing.can_confirm || pending}
          onChange={(event) => setCode(event.target.value)}
        />
      </div>
      <div
        className="flex flex-wrap gap-1.5"
        role="group"
        aria-label={`Part ${part.position} on page ${page}, drawing ${drawing.tag}`}
      >
        {PART_KINDS.map((kind) => (
          <Button
            key={kind}
            type="button"
            size="sm"
            variant={choiceVariant(confirmedKind === kind)}
            aria-pressed={confirmedKind === kind}
            disabled={!drawing.can_confirm || pending}
            onClick={() => onConfirm(part, kind, codeToSend(code))}
          >
            <ChoiceMark chosen={confirmedKind === kind} />
            {KIND_LABEL[kind]}
          </Button>
        ))}
        <Button
          type="button"
          size="sm"
          variant={part.decision?.decision === 'withdrawn' ? 'default' : 'ghost'}
          aria-pressed={part.decision?.decision === 'withdrawn'}
          disabled={pending}
          onClick={() => onWithdraw(part)}
        >
          <ChoiceMark chosen={part.decision?.decision === 'withdrawn'} />
          Not a part
        </Button>
      </div>
      <DecisionLine feedback={feedback} />
    </li>
  );
}

/**
 * A part the suggestions missed, placed by its ends on the stored vendor-only page picture. Folded
 * away by default (#1124): it is the exception, and open it pushed every part below the fold.
 */
function AddPartForm({
  loadPagePicture,
  drawing,
  saving,
  feedback,
  onAdd,
}: {
  loadPagePicture: (viewId: string) => Promise<Blob>;
  drawing: PartDrawing;
  saving: boolean;
  feedback?: DecisionFeedback;
  onAdd: (drawing: PartDrawing, part: NewPart) => void;
}) {
  const [kind, setKind] = useState<PartKind>('filler');
  const [code, setCode] = useState('');
  const kindId = `add-part-kind-${drawing.view_id}`;
  const codeId = `add-part-code-${drawing.view_id}`;

  return (
    <details className="group rounded-xl border border-dashed px-4 py-3">
      <summary className="cursor-pointer text-sm font-medium">Add a part the suggestions missed</summary>
      <div className="mt-3 flex flex-col gap-3">
        <Hint>Choose what it is, then mark its two ends on the vendor&apos;s drawing.</Hint>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="flex flex-col gap-1">
            <label htmlFor={kindId} className="text-xs text-muted-foreground">It is a</label>
            <select id={kindId} className={SELECT_CLASS} value={kind} disabled={saving} onChange={(event) => setKind(event.target.value as PartKind)}>
              {PART_KINDS.map((option) => (
                <option key={option} value={option}>
                  {KIND_LABEL[option].toLowerCase()}
                </option>
              ))}
            </select>
          </div>
          <div className="flex flex-col gap-1">
            <label htmlFor={codeId} className="text-xs text-muted-foreground">Code as printed (leave empty if none)</label>
            <input
              id={codeId}
              type="text"
              className={`${INPUT_CLASS} num`}
              value={code}
              maxLength={200}
              disabled={saving}
              onChange={(event) => setCode(event.target.value)}
            />
          </div>
        </div>
        <VendorPagePlacement
          loadPagePicture={loadPagePicture}
          drawing={drawing}
          kind={kind}
          code={codeToSend(code)}
          disabled={saving}
          onAdd={(part) => onAdd(drawing, part)}
        />
        <DecisionLine feedback={feedback} />
      </div>
    </details>
  );
}

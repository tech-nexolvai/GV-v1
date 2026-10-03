import { useState, type FormEvent, type ReactNode } from 'react';
import { PartDecisionFeedback } from './PartDecisionFeedback.js';
import type { MeasurementDecisionState as PartDecisionState } from './measurementDecisionSave.js';

import {
  KIND_LABEL,
  PART_KINDS,
  codeToSend,
  decisionLabel,
  drawingLabel,
  endChoices,
  startingCode,
  stillToDecide,
  type PartDrawing,
  type PartKind,
  type PartPoint,
  type SuggestedPart,
} from './drawingPartChoices.js';

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
 */
export function DrawingPartsList({
  drawings,
  decisions = {},
  renderCrop,
  onConfirm,
  onWithdraw,
  onAdd,
}: {
  drawings: readonly PartDrawing[];
  /** Independent state per part, or per drawing while adding, never one shared pending row. */
  decisions?: Readonly<Record<string, PartDecisionState | undefined>>;
  /** The stored picture of a part that has one. */
  renderCrop: (drawing: PartDrawing, part: SuggestedPart) => ReactNode;
  onConfirm: (part: SuggestedPart, kind: PartKind, code: string | null) => void;
  onWithdraw: (part: SuggestedPart) => void;
  onAdd: (drawing: PartDrawing, part: NewPart) => void;
}) {
  const open = stillToDecide(drawings);
  return (
    <section className="enter-values__section drawing-parts" aria-labelledby="drawing-parts-title">
      <h2 id="drawing-parts-title">Parts of each drawing</h2>
      <p className="enter-values__hint">
        The computer suggests the parts it finds in each vendor&apos;s drawing. A suggestion counts
        for nothing until you say what it is. Decide each one on its own: say whether it is a cabinet,
        a filler or a countertop (a filler is drawn like a cabinet, so it is suggested as one), correct
        its code if the drawing prints it differently, or say it is not a part.{' '}
        <strong>{open === 0 ? 'Nothing left to decide.' : `${open} still to decide.`}</strong>
      </p>
      {drawings.map((drawing) => (
        <article className="drawing-parts__drawing" key={drawing.view_id}>
          <h3>
            Page {drawing.page_index + 1}: {drawingLabel(drawing)}
          </h3>
          {drawing.why_not && <p className="drawing-parts__why-not">{drawing.why_not}</p>}
          {drawing.parts.length === 0 ? (
            <p className="enter-values__hint">Nothing was suggested on this drawing.</p>
          ) : (
            <ol className="drawing-parts__list">
              {drawing.parts.map((part) => (
                <PartRow
                  key={part.proposal_id}
                  drawing={drawing}
                  part={part}
                  state={decisions[part.proposal_id]}
                  crop={part.has_crop ? renderCrop(drawing, part) : null}
                  onConfirm={onConfirm}
                  onWithdraw={onWithdraw}
                />
              ))}
            </ol>
          )}
          {drawing.can_confirm && (
            <AddPartForm drawing={drawing} state={decisions[drawing.view_id]} onAdd={onAdd} />
          )}
        </article>
      ))}
    </section>
  );
}

function PartRow({
  drawing,
  part,
  state,
  crop,
  onConfirm,
  onWithdraw,
}: {
  drawing: PartDrawing;
  part: SuggestedPart;
  state?: PartDecisionState;
  crop: ReactNode;
  onConfirm: (part: SuggestedPart, kind: PartKind, code: string | null) => void;
  onWithdraw: (part: SuggestedPart) => void;
}) {
  const [code, setCode] = useState(() => startingCode(part));
  const saving = state?.kind === 'saving';
  const page = drawing.page_index + 1;
  const confirmedKind = part.decision?.decision === 'confirmed' ? part.decision.kind : null;
  return (
    <li className="drawing-parts__item" data-decided={part.decision !== null}>
      <div className="drawing-parts__picture">
        {crop ?? (
          <p className="drawing-parts__no-picture">
            No picture of this part is stored yet. Find it on page {page}: it is number{' '}
            {part.position} from the left in this drawing.
          </p>
        )}
      </div>
      <div className="drawing-parts__facts">
        <strong>
          {part.position}. Suggested as a {KIND_LABEL[part.suggested_kind].toLowerCase()}
          {part.added_by_a_person ? ' (added by a person)' : ''}
        </strong>
        <span>{part.reason}</span>
        <span>
          {part.suggested_code ? `Code read on the drawing: “${part.suggested_code}”` : 'No code read.'}
        </span>
        <span className="drawing-parts__decision">{decisionLabel(part)}</span>
      </div>
      <label className="drawing-parts__code">
        Code as printed (leave empty if none)
        <input
          type="text"
          value={code}
          maxLength={200}
          disabled={!drawing.can_confirm || saving}
          onChange={(event) => setCode(event.target.value)}
        />
      </label>
      <div
        className="drawing-parts__choices"
        role="group"
        aria-label={`Part ${part.position} on page ${page}, drawing ${drawing.tag}`}
      >
        {PART_KINDS.map((kind) => (
          <button
            key={kind}
            type="button"
            className={`btn btn--sm ${confirmedKind === kind ? 'btn--primary' : 'btn--subtle'}`}
            aria-pressed={confirmedKind === kind}
            disabled={!drawing.can_confirm || saving}
            onClick={() => onConfirm(part, kind, codeToSend(code))}
          >
            {KIND_LABEL[kind]}
          </button>
        ))}
        <button
          type="button"
          className={`btn btn--sm ${part.decision?.decision === 'withdrawn' ? 'btn--primary' : 'btn--subtle'}`}
          aria-pressed={part.decision?.decision === 'withdrawn'}
          disabled={saving}
          onClick={() => onWithdraw(part)}
        >
          Not a part
        </button>
      </div>
      <PartDecisionFeedback state={state} />
    </li>
  );
}

/**
 * A part the suggestions missed, between two ends the drawing's listed parts already have.
 *
 * Only those ends, because no picture of the whole drawing is stored to point at: a person names
 * the part by where it starts and stops among the parts they can see listed.
 */
function AddPartForm({
  drawing,
  state,
  onAdd,
}: {
  drawing: PartDrawing;
  state?: PartDecisionState;
  onAdd: (drawing: PartDrawing, part: NewPart) => void;
}) {
  const ends = endChoices(drawing);
  const saving = state?.kind === 'saving';
  const [kind, setKind] = useState<PartKind>('filler');
  const [code, setCode] = useState('');
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const start = ends.find((end) => end.key === from);
  const stop = ends.find((end) => end.key === to);

  if (ends.length < 2) {
    return (
      <p className="enter-values__hint">
        No part is listed on this drawing to take two ends from, so a part cannot be added here yet.
      </p>
    );
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!start || !stop || start.key === stop.key) return;
    onAdd(drawing, { kind, code: codeToSend(code), ends: [start.point, stop.point] });
  }

  return (
    <form className="drawing-parts__add" onSubmit={submit}>
      <h4>Add a part the suggestions missed</h4>
      <p className="enter-values__hint">
        Pick where it starts and where it stops, from the ends of the parts listed above.
      </p>
      <label>
        It is a
        <select value={kind} disabled={saving} onChange={(event) => setKind(event.target.value as PartKind)}>
          {PART_KINDS.map((option) => (
            <option key={option} value={option}>
              {KIND_LABEL[option].toLowerCase()}
            </option>
          ))}
        </select>
      </label>
      <label>
        From the
        <select value={from} disabled={saving} onChange={(event) => setFrom(event.target.value)}>
          <option value="">choose an end</option>
          {ends.map((end) => (
            <option key={end.key} value={end.key}>
              {end.label}
            </option>
          ))}
        </select>
      </label>
      <label>
        to the
        <select value={to} disabled={saving} onChange={(event) => setTo(event.target.value)}>
          <option value="">choose an end</option>
          {ends.map((end) => (
            <option key={end.key} value={end.key}>
              {end.label}
            </option>
          ))}
        </select>
      </label>
      <label>
        Code as printed (leave empty if none)
        <input
          type="text"
          value={code}
          maxLength={200}
          disabled={saving}
          onChange={(event) => setCode(event.target.value)}
        />
      </label>
      <button
        type="submit"
        className="btn btn--sm btn--subtle"
        disabled={saving || !start || !stop || start.key === stop.key}
      >
        Add this part
      </button>
      <PartDecisionFeedback state={state} />
    </form>
  );
}

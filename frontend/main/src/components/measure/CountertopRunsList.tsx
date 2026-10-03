import { useState } from 'react';
import { CountertopRunFeedback } from './CountertopRunFeedback.js';
import type { MeasurementDecisionState } from './measurementDecisionSave.js';

import {
  isTheSuggestion,
  partLabel,
  runDecisionLabel,
  runsStillToDecide,
  startingSelection,
  type RunCountertop,
  type RunDrawing,
  type RunsList,
} from './countertopRunChoices.js';

/**
 * Which confirmed parts sit beneath each countertop: what the computer suggests, and a person
 * deciding each countertop's run (#893).
 *
 * **Why a person is asked.** The countertop check adds up the cabinets and fillers beneath a top,
 * so which parts those are decides the sum. The computer suggests them from where each part is drawn,
 * leaving out anything drawn above the top, as a wall cabinet is; a person confirms the run, corrects
 * it by ticking different parts, or says it is wrong.
 *
 * **The order is the drawing's.** A person only says which parts; the server orders them left to
 * right across the drawing, never in the order they were ticked.
 *
 * **One countertop at a time.** Each countertop has its own boxes and buttons, and there is
 * deliberately no "confirm all".
 */
export function CountertopRunsList({
  runs,
  decisions = {},
  locked = false,
  onConfirm,
  onWithdraw,
}: {
  runs: RunsList;
  decisions?: Readonly<Record<string, MeasurementDecisionState | undefined>>;
  /** A refused refresh leaves the old list visible, but not actionable until retried. */
  locked?: boolean;
  onConfirm: (countertop: RunCountertop, partIds: string[]) => void;
  onWithdraw: (countertop: RunCountertop) => void;
}) {
  const open = runsStillToDecide(runs);
  return (
    <section className="enter-values__section drawing-parts" aria-labelledby="countertop-runs-title">
      <h2 id="countertop-runs-title">Parts under each countertop</h2>
      <p className="enter-values__hint">
        For each countertop you confirmed above, the computer suggests the cabinets and fillers that
        sit beneath it, left to right, leaving out anything drawn above the top, as a wall cabinet
        is. A suggestion counts for nothing until you decide it. Confirm the run, tick different
        parts and confirm that instead, or say it is not this countertop&apos;s run.{' '}
        {runs.can_suggest && (
          <strong>{open === 0 ? 'Nothing left to decide.' : `${open} still to decide.`}</strong>
        )}
      </p>
      {runs.why_not && <p className="drawing-parts__why-not">{runs.why_not}</p>}
      {runs.drawings.map((drawing) => (
        <article className="drawing-parts__drawing" key={drawing.view_id}>
          <h3>Page {drawing.page_index + 1}: the vendor&apos;s drawing</h3>
          {drawing.why_not && <p className="drawing-parts__why-not">{drawing.why_not}</p>}
          {drawing.countertops.length === 0 ? (
            <p className="enter-values__hint">
              No countertop is confirmed on this drawing, so there is no run to decide.
            </p>
          ) : (
            <ol className="drawing-parts__list">
              {drawing.countertops.map((countertop) => (
                <CountertopRow
                  // A new decision starts the boxes again from what was decided.
                  key={`${countertop.countertop_item_id}:${countertop.decision?.decided_at ?? ''}`}
                  canSuggest={runs.can_suggest}
                  drawing={drawing}
                  countertop={countertop}
                  state={decisions[countertop.countertop_item_id]}
                  locked={locked}
                  onConfirm={onConfirm}
                  onWithdraw={onWithdraw}
                />
              ))}
            </ol>
          )}
        </article>
      ))}
    </section>
  );
}

function CountertopRow({
  canSuggest,
  drawing,
  countertop,
  state,
  locked,
  onConfirm,
  onWithdraw,
}: {
  canSuggest: boolean;
  drawing: RunDrawing;
  countertop: RunCountertop;
  state?: MeasurementDecisionState;
  locked: boolean;
  onConfirm: (countertop: RunCountertop, partIds: string[]) => void;
  onWithdraw: (countertop: RunCountertop) => void;
}) {
  const [selected, setSelected] = useState<string[]>(() => startingSelection(countertop));
  const saving = state?.kind === 'saving' || locked;
  const suggestion = countertop.suggestion;
  const decision = countertop.decision;
  const name = countertop.number === null ? 'A countertop' : `Countertop, part ${countertop.number}`;
  // Only parts still on the drawing: one taken back since the boxes were ticked is never sent.
  const ticked = selected.filter((id) => drawing.parts.some((part) => part.item_id === id));
  const canConfirm = canSuggest && drawing.can_confirm && ticked.length > 0 && !saving;

  function toggle(itemId: string, ticked: boolean) {
    setSelected((current) =>
      ticked ? [...current.filter((id) => id !== itemId), itemId] : current.filter((id) => id !== itemId),
    );
  }

  return (
    <li className="drawing-parts__item" data-decided={decision !== null}>
      <div className="drawing-parts__facts">
        <strong>{name}</strong>
        {suggestion && (
          <>
            <span>Suggested run, left to right:</span>
            {suggestion.members.length === 0 ? (
              <span>Nothing is suggested beneath this countertop.</span>
            ) : (
              <ol className="countertop-runs__members">
                {suggestion.members.map((member) => (
                  <li key={member.item_id}>
                    {partLabel(member)}: {member.signal}
                  </li>
                ))}
              </ol>
            )}
            {suggestion.left_out.map((entry) => (
              <span key={entry.item_id}>
                {partLabel(entry)}: {entry.reason}
              </span>
            ))}
            {suggestion.warnings.map((warning) => (
              <span key={warning} className="countertop-runs__warning">
                {warning}
              </span>
            ))}
          </>
        )}
        <span className="drawing-parts__decision">{runDecisionLabel(countertop)}</span>
        {decision?.why_not_read && (
          <span className="countertop-runs__warning" role="status">
            {decision.why_not_read}
          </span>
        )}
      </div>
      <fieldset className="countertop-runs__pick" disabled={!drawing.can_confirm || saving}>
        <legend>Parts in this countertop&apos;s run</legend>
        {drawing.parts.map((part) => (
          <label key={part.item_id}>
            <input
              type="checkbox"
              checked={selected.includes(part.item_id)}
              onChange={(event) => toggle(part.item_id, event.target.checked)}
            />
            {partLabel(part)}
          </label>
        ))}
      </fieldset>
      <div
        className="drawing-parts__choices"
        role="group"
        aria-label={`The run under ${name.toLowerCase()} on page ${drawing.page_index + 1}`}
      >
        <button
          type="button"
          className="btn btn--sm btn--subtle"
          disabled={!canConfirm}
          onClick={() => onConfirm(countertop, ticked)}
        >
          {isTheSuggestion(countertop, ticked) ? 'Confirm this run' : 'Confirm the ticked parts'}
        </button>
        <button
          type="button"
          className={`btn btn--sm ${decision?.decision === 'withdrawn' ? 'btn--primary' : 'btn--subtle'}`}
          aria-pressed={decision?.decision === 'withdrawn'}
          disabled={saving}
          onClick={() => onWithdraw(countertop)}
        >
          Not this countertop&apos;s run
        </button>
      </div>
      <CountertopRunFeedback state={state} />
    </li>
  );
}

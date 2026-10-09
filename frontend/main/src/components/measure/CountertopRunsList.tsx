import { useState } from 'react';
import type { DecisionFeedback } from './decisionFeedback.js';

import {
  isTheSuggestion,
  partLabel,
  runDecisionLabel,
  runsStillToDecide,
  startingSelection,
  wallLayoutLabel,
  type RunCountertop,
  type RunDrawing,
  type RunsList,
} from './countertopRunChoices.js';
import { Button } from '@/components/ui/button';
import { Caution, ChoiceMark, DecisionLine, Hint, SELECT_CLASS, StepSection } from './wizard-ui.js';
import { countWords } from './wizardWords.js';

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
  saving,
  feedback,
  onConfirm,
  onWithdraw,
}: {
  runs: RunsList;
  /** The countertop being saved, so its buttons cannot be pressed twice. */
  saving: string | null;
  feedback?: Readonly<Record<string, DecisionFeedback>>;
  onConfirm: (countertop: RunCountertop, partIds: string[], wallConfig: string) => void;
  onWithdraw: (countertop: RunCountertop) => void;
}) {
  const open = runsStillToDecide(runs);
  return (
    <StepSection
      id="countertop-runs-title"
      slot="countertop-runs"
      title="Parts under each countertop"
      line={
        runs.can_suggest ? (
          <strong className="font-medium text-foreground">
            {open === 0 ? 'Nothing left to decide.' : `${countWords(open, 'countertop')} still to decide.`}
          </strong>
        ) : (
          'Decide the parts under each countertop.'
        )
      }
      tipLabel="About countertop runs"
      tip={
        <>
          <p>For each countertop you confirmed in step 1, the computer suggests the cabinets and fillers that sit beneath it, left to right, leaving out anything drawn above the top, as a wall cabinet is.</p>
          <p>A suggestion counts for nothing until you decide it. Confirm the run, tick different parts and confirm that instead, or say it is not this countertop&apos;s run.</p>
        </>
      }
    >
      {runs.why_not && <Hint>{runs.why_not}</Hint>}
      {runs.drawings.map((drawing) => (
        <article className="flex flex-col gap-3" key={drawing.view_id}>
          <h4 className="text-sm font-medium">Page {drawing.page_index + 1}: the vendor&apos;s drawing</h4>
          {drawing.why_not && <Hint>{drawing.why_not}</Hint>}
          {drawing.countertops.length === 0 ? (
            <Hint>No countertop is confirmed on this drawing, so there is no run to decide.</Hint>
          ) : (
            <ol className="grid gap-3 lg:grid-cols-2">
              {drawing.countertops.map((countertop) => (
                <CountertopRow
                  // A new decision starts the boxes again from what was decided.
                  key={`${countertop.countertop_item_id}:${countertop.decision?.decided_at ?? ''}`}
                  canSuggest={runs.can_suggest}
                  drawing={drawing}
                  countertop={countertop}
                  wallLayoutChoices={runs.wall_layout_choices}
                  saving={saving === countertop.countertop_item_id}
                  feedback={feedback?.[countertop.countertop_item_id]}
                  onConfirm={onConfirm}
                  onWithdraw={onWithdraw}
                />
              ))}
            </ol>
          )}
        </article>
      ))}
    </StepSection>
  );
}

function CountertopRow({
  canSuggest,
  drawing,
  countertop,
  wallLayoutChoices,
  saving,
  feedback,
  onConfirm,
  onWithdraw,
}: {
  canSuggest: boolean;
  drawing: RunDrawing;
  countertop: RunCountertop;
  wallLayoutChoices: string[];
  saving: boolean;
  feedback?: DecisionFeedback;
  onConfirm: (countertop: RunCountertop, partIds: string[], wallConfig: string) => void;
  onWithdraw: (countertop: RunCountertop) => void;
}) {
  const [selected, setSelected] = useState<string[]>(() => startingSelection(countertop));
  const [wallSelection, setWallSelection] = useState<{ value: string } | null>(null);
  // Never the readers' suggestion on its own (DECIDED: nothing pre-selected): the layout is the one a
  // person chose before, or chooses now — "Use the suggested layout" is that choice, made explicitly.
  const wallConfig = countertop.decision?.wall_config ?? wallSelection?.value ?? '';
  const proposal = countertop.wall_layout_proposal;
  const suggestion = countertop.suggestion;
  const decision = countertop.decision;
  const name = countertop.number === null ? 'A countertop' : `Countertop, part ${countertop.number}`;
  // Only parts still on the drawing: one taken back since the boxes were ticked is never sent.
  const ticked = selected.filter((id) => drawing.parts.some((part) => part.item_id === id));
  const pending = saving || feedback?.kind === 'saving';
  const canConfirm = canSuggest && drawing.can_confirm && ticked.length > 0 && wallConfig !== '' && !pending;
  const layoutId = `run-layout-${countertop.countertop_item_id}`;

  function toggle(itemId: string, ticked: boolean) {
    setSelected((current) =>
      ticked ? [...current.filter((id) => id !== itemId), itemId] : current.filter((id) => id !== itemId),
    );
  }

  return (
    <li
      data-slot="countertop-run"
      data-decided={decision !== null}
      className="flex min-w-0 flex-col gap-3 rounded-xl border bg-card p-4 data-[decided=true]:bg-muted/40"
    >
      <div className="flex flex-col gap-1">
        <p className="text-sm font-medium">{name}</p>
        {suggestion && (
          <>
            <Hint>Suggested run, left to right:</Hint>
            {suggestion.members.length === 0 ? (
              <Hint>Nothing is suggested beneath this countertop.</Hint>
            ) : (
              <ol data-slot="run-members" className="list-decimal pl-5 text-xs text-muted-foreground">
                {suggestion.members.map((member) => (
                  <li key={member.item_id}>
                    {partLabel(member)}: {member.signal}
                  </li>
                ))}
              </ol>
            )}
            {suggestion.left_out.map((entry) => (
              <Hint key={entry.item_id}>
                {partLabel(entry)}: {entry.reason}
              </Hint>
            ))}
            {suggestion.warnings.map((warning) => (
              <Caution key={warning} role="note">{warning}</Caution>
            ))}
          </>
        )}
        <p className="text-xs font-medium">{runDecisionLabel(countertop)}</p>
        {decision?.why_not_read && <Caution>{decision.why_not_read}</Caution>}
      </div>
      <fieldset className="flex flex-col gap-1.5 disabled:opacity-60" disabled={!drawing.can_confirm || pending}>
        <legend className="mb-1 text-xs text-muted-foreground">Parts in this countertop&apos;s run</legend>
        <div className="flex flex-wrap gap-x-4 gap-y-1.5">
          {drawing.parts.map((part) => (
            <label key={part.item_id} className="flex min-h-8 items-center gap-2 text-sm">
              <input
                type="checkbox"
                className="size-4 accent-foreground"
                checked={selected.includes(part.item_id)}
                onChange={(event) => toggle(part.item_id, event.target.checked)}
              />
              {partLabel(part)}
            </label>
          ))}
        </div>
      </fieldset>
      <div className="flex flex-col gap-1">
        <label htmlFor={layoutId} className="text-xs text-muted-foreground">
          Wall layout for this countertop (required)
        </label>
        <select
          id={layoutId}
          className={SELECT_CLASS}
          value={wallConfig}
          disabled={pending}
          onChange={(event) => {
            setWallSelection({ value: event.target.value });
          }}
          required
        >
          <option value="">Choose this countertop&apos;s wall layout</option>
          {wallLayoutChoices.map((choice) => (
            <option key={choice} value={choice}>
              {wallLayoutLabel(choice)}
            </option>
          ))}
        </select>
        {proposal && (
          <div className="flex flex-col items-start gap-1.5">
            <Hint role="status">
              Suggested from {proposal.source === 'readers' ? 'the drawing readers' : 'drawing clues'}:
              {' '}{wallLayoutLabel(proposal.value)}. Choose it, then confirm this run to use it.
            </Hint>
            {wallConfig !== proposal.value && (
              <Button
                type="button"
                size="xs"
                variant="outline"
                disabled={pending}
                onClick={() => setWallSelection({ value: proposal.value })}
              >
                Use the suggested layout: {wallLayoutLabel(proposal.value)}
              </Button>
            )}
          </div>
        )}
        {wallConfig === '' && <Hint>Choose a layout first; nothing is selected for you.</Hint>}
      </div>
      <div
        className="flex flex-wrap gap-2"
        role="group"
        aria-label={`The run under ${name.toLowerCase()} on page ${drawing.page_index + 1}`}
      >
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={!canConfirm}
          onClick={() => onConfirm(countertop, ticked, wallConfig)}
        >
          {isTheSuggestion(countertop, ticked) ? 'Confirm this run' : 'Confirm the ticked parts'}
        </Button>
        <Button
          type="button"
          size="sm"
          variant={decision?.decision === 'withdrawn' ? 'default' : 'ghost'}
          aria-pressed={decision?.decision === 'withdrawn'}
          disabled={pending}
          onClick={() => onWithdraw(countertop)}
        >
          <ChoiceMark chosen={decision?.decision === 'withdrawn'} />
          Not this countertop&apos;s run
        </Button>
      </div>
      <DecisionLine feedback={feedback} />
    </li>
  );
}

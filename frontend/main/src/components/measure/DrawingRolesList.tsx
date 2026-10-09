import {
  DRAWING_ROLES,
  ROLE_LABEL,
  roleLabel,
  stillToConfirm,
  type DrawingRole,
  type DrawingView,
} from './drawingRoleChoices.js';
import type { DecisionFeedback } from './decisionFeedback.js';
import { Button } from '@/components/ui/button';
import { ChoiceMark, DecisionLine, StepSection } from './wizard-ui.js';
import { choiceVariant, countWords } from './wizardWords.js';

/**
 * Which drawing is which, on sheets that hold both (#795).
 *
 * **Why the reviewer is asked.** The client's sheets put the architect's `ID SET ELEVATION` beside the
 * vendor's shop elevation, so the upload cannot say which half a number sits in. A reading is used
 * only on the side of the drawing it is in, and a reading on a drawing nobody has confirmed is used on
 * neither — so this list is what lets the readings on a combined sheet fill a field at all.
 *
 * **The sheet's label is shown, never applied.** The suggestion is what the printed heading reads
 * as; the buttons are the only thing that sets a role, one drawing at a time. There is deliberately
 * no "confirm all": each one is a person saying which drawing they are looking at.
 */
export function DrawingRolesList({
  views,
  saving,
  feedback,
  onChoose,
}: {
  views: readonly DrawingView[];
  /** The drawing whose answer is being saved, so its buttons cannot be pressed twice. */
  saving: string | null;
  feedback?: Readonly<Record<string, DecisionFeedback>>;
  onChoose: (view: DrawingView, role: DrawingRole) => void;
}) {
  const open = stillToConfirm(views);
  return (
    <StepSection
      id="drawing-roles-title"
      slot="drawing-roles"
      title="Which drawing is which?"
      line={
        <strong className="font-medium text-foreground">
          {open === 0 ? `All ${countWords(views.length, 'drawing')} confirmed.` : `${countWords(open, 'drawing')} still to confirm.`}
        </strong>
      }
      tipLabel="About drawing roles"
      tip={
        <>
          <p>These sheets show the architect&apos;s drawing and the vendor&apos;s side by side. Say which side of each sheet is whose.</p>
          <p>A reading is used only on the side of the drawing it sits in, and a reading on a drawing nobody has confirmed is used on neither.</p>
        </>
      }
    >
      <ul className="divide-y rounded-xl border bg-card">
        {views.map((view) => {
          const suggested = roleLabel(view.suggested_role);
          const fromUpload = view.role === null ? roleLabel(view.upload_side) : null;
          return (
            <li
              key={view.view_id}
              data-confirmed={view.role !== null}
              className="flex flex-col gap-2 px-4 py-3 sm:flex-row sm:flex-wrap sm:items-center sm:justify-between"
            >
              <div className="flex min-w-0 flex-col gap-0.5">
                <p className="text-sm">
                  <span className="font-medium">
                    Page <span className="num">{view.page_index + 1}</span>
                  </span>
                  <span className="text-muted-foreground">
                    {' · '}
                    {view.suggested_from ? `labelled “${view.suggested_from.trim()}”` : 'no label read on the sheet'}
                  </span>
                </p>
                {fromUpload ? (
                  <p className="text-xs text-muted-foreground">from the upload: {fromUpload} (confirm to change it)</p>
                ) : (
                  view.role === null &&
                  suggested && <p className="text-xs text-muted-foreground">the label suggests: {suggested}</p>
                )}
              </div>
              <div
                className="flex shrink-0 flex-wrap gap-2"
                role="group"
                aria-label={`Page ${view.page_index + 1}, drawing ${view.tag}`}
              >
                {DRAWING_ROLES.map((role) => (
                  <Button
                    key={role}
                    type="button"
                    size="sm"
                    variant={choiceVariant(view.role === role)}
                    aria-pressed={view.role === role}
                    disabled={saving === view.view_id || feedback?.[view.view_id]?.kind === 'saving'}
                    onClick={() => onChoose(view, role)}
                  >
                    <ChoiceMark chosen={view.role === role} />
                    {ROLE_LABEL[role]}
                  </Button>
                ))}
              </div>
              {feedback?.[view.view_id] && (
                <div className="sm:basis-full">
                  <DecisionLine feedback={feedback[view.view_id]} />
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </StepSection>
  );
}

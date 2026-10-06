import {
  DRAWING_ROLES,
  ROLE_LABEL,
  roleLabel,
  stillToConfirm,
  type DrawingRole,
  type DrawingView,
} from './drawingRoleChoices.js';
import { feedbackText, type DecisionFeedback } from './decisionFeedback.js';

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
    <section className="enter-values__section drawing-roles" aria-labelledby="drawing-roles-title">
      <h2 id="drawing-roles-title">Which drawing is which?</h2>
      <p className="enter-values__hint">
        These sheets show the architect&apos;s drawing and the vendor&apos;s side by side. A reading
        is used only on the side of the drawing it sits in, and a reading on a drawing nobody has
        confirmed is used on neither.{' '}
        <strong>{open === 0 ? 'All confirmed.' : `${open} still to confirm.`}</strong>
      </p>
      <ul className="drawing-roles__list">
        {views.map((view) => {
          const suggested = roleLabel(view.suggested_role);
          const fromUpload = view.role === null ? roleLabel(view.upload_side) : null;
          return (
            <li className="drawing-roles__item" key={view.view_id} data-confirmed={view.role !== null}>
              <div className="drawing-roles__facts">
                <strong>Page {view.page_index + 1}</strong>
                <span>
                  {view.suggested_from
                    ? `labelled “${view.suggested_from.trim()}”`
                    : 'no label read on the sheet'}
                </span>
                {fromUpload ? (
                  <span className="drawing-roles__suggested">
                    from the upload: {fromUpload} (confirm to change it)
                  </span>
                ) : (
                  view.role === null &&
                  suggested && (
                    <span className="drawing-roles__suggested">the label suggests: {suggested}</span>
                  )
                )}
              </div>
              <div
                className="drawing-roles__choices"
                role="group"
                aria-label={`Page ${view.page_index + 1}, drawing ${view.tag}`}
              >
                {DRAWING_ROLES.map((role) => (
                  <button
                    key={role}
                    type="button"
                    className={`btn btn--sm ${view.role === role ? 'btn--primary' : 'btn--subtle'}`}
                    aria-pressed={view.role === role}
                    disabled={saving === view.view_id || feedback?.[view.view_id]?.kind === 'saving'}
                    onClick={() => onChoose(view, role)}
                  >
                    {ROLE_LABEL[role]}
                  </button>
                ))}
              </div>
              {feedback?.[view.view_id] && (
                <p className="drawing-parts__feedback" role={feedback[view.view_id].kind === 'error' ? 'alert' : 'status'}>
                  {feedbackText(feedback[view.view_id])}
                </p>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

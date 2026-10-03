export type ReadingConfirmationState =
  | { kind: 'saving' }
  | { kind: 'error'; message: string }
  | { kind: 'confirmed'; preserved: boolean; multiple: boolean };

/** Feedback belongs to the field the reviewer clicked, not a distant inspection section. */
export function ReadingConfirmationFeedback({ state, id }: {
  state?: ReadingConfirmationState;
  id: string;
}) {
  if (!state) return null;
  if (state.kind === 'error') return (
    <p id={id} className="enter-values__error" role="alert">
      Confirmation was not completed. {state.message} The field was not changed.
    </p>
  );
  return (
    <p id={id} className="enter-values__hint enter-values__hint--tight" role="status">
      {state.kind === 'saving' ? 'Saving confirmation… The field will update after the server accepts it.'
        : state.preserved ? 'Reading confirmed. Your edited value has been kept in this field.'
          : state.multiple ? 'Reading confirmed. There is more than one confirmed reading for this field; enter the value you intend to use.'
            : 'Reading confirmed and added to this field.'}
    </p>
  );
}

/**
 * "What is this drawing set for?" — the product the reviewer picks at upload (#994).
 *
 * The choices come from the API (`GET /product-types`), never from a list in this file: a product
 * appears only once the published rulebook has a check for it, so the dropdown cannot offer a
 * product nothing would check. Only that product's checks run, and the readers are told it.
 *
 * Presentational on purpose: the form owns loading and the chosen value, so this renders the same
 * on the server as in the browser and its test needs no network.
 */

import type { ProductChoice, ProductType } from '../../api/uploadState';

export type ProductChoicesState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; choices: readonly ProductChoice[] };

interface ProductTypeFieldProps {
  state: ProductChoicesState;
  value: ProductType | null;
  onChange: (value: ProductType) => void;
  disabled?: boolean;
}

export function ProductTypeField({ state, value, onChange, disabled = false }: ProductTypeFieldProps) {
  const choices = state.status === 'ready' ? state.choices : [];
  return (
    <label className="new-review__product">
      <span className="new-review__label">What is this drawing set for?</span>
      <select
        className="new-review__product-select"
        value={value ?? ''}
        disabled={disabled || state.status !== 'ready' || choices.length === 0}
        onChange={(event) => {
          const chosen = choices.find((choice) => choice.value === event.target.value);
          if (chosen) onChange(chosen.value);
        }}
        required
      >
        {state.status === 'loading' && <option value="">Loading the products…</option>}
        {state.status === 'ready' && choices.length === 0 && (
          <option value="">No checks are published yet</option>
        )}
        {choices.map((choice) => (
          <option key={choice.value} value={choice.value}>
            {choice.label}
          </option>
        ))}
      </select>
      {state.status === 'error' && (
        <span className="new-review__product-note" role="alert">
          The list of products could not be loaded: {state.message}
        </span>
      )}
      {state.status === 'ready' && choices.length === 0 && (
        <span className="new-review__product-note">
          No checks are published yet, so a drawing set could not be checked. Publish the rulebook
          first.
        </span>
      )}
      {state.status === 'ready' && choices.length > 0 && (
        <span className="new-review__product-note">
          Only the checks for this product are run.
        </span>
      )}
    </label>
  );
}

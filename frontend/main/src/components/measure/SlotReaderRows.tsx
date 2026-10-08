import { useEffect, useState } from 'react';

import {
  ApiError,
  listSlotReaderRows,
  reviewSlotReaderRow,
  type SlotReaderRow,
} from '../../api/client';
import { projectId } from '../../api/config';
import { rowWallSelection, shouldOfferRowWallControl, slotReaderReviewPayload, type SlotReaderReviewPayload } from './slotReaderReview.js';
import './SlotReaderRows.css';

/** One reviewer decision per slot-reader row; no value or wall choice is revision-wide. */
export function SlotReaderRows({ packageId, refresh, targetRow, onTargetReached, onReviewRow }: { packageId: string; refresh: number; targetRow?: string | null; onTargetReached?: () => void; onReviewRow?: (rowId: string) => void }) {
  const [rows, setRows] = useState<SlotReaderRow[]>([]);
  const [wallDrafts, setWallDrafts] = useState<Record<string, string>>({});
  const [valueDrafts, setValueDrafts] = useState<Record<string, Record<string, string>>>({});
  const [saving, setSaving] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let live = true;
    listSlotReaderRows(projectId(), packageId)
      .then((result) => {
        if (live) {
          setRows(result.rows);
          setError(null);
        }
      })
      .catch((caught: unknown) => {
        if (live) setError(caught instanceof ApiError ? caught.message : String(caught));
      });
    return () => {
      live = false;
    };
  }, [attempt, packageId, refresh]);

  useEffect(() => {
    if (!targetRow || !rows.some(row => row.row_id === targetRow)) return;
    const target = document.getElementById(`slot-row-${targetRow}`);
    target?.scrollIntoView({ block: 'start' });
    target?.focus({ preventScroll: true });
    onTargetReached?.();
  }, [targetRow, rows, onTargetReached]);

  async function save(row: SlotReaderRow, wallConfirmation?: SlotReaderReviewPayload) {
    const payload = wallConfirmation ?? slotReaderReviewPayload(row, wallDrafts[row.row_id], valueDrafts[row.row_id]);
    if (!payload.wall_config && !payload.measurements) return;

    setSaving(row.row_id);
    setFeedback((current) => ({ ...current, [row.row_id]: '' }));
    try {
      await reviewSlotReaderRow(projectId(), packageId, row.row_id, {
        ...payload,
      });
      if (!wallConfirmation) setValueDrafts((current) => ({ ...current, [row.row_id]: {} }));
      if (wallConfirmation) setWallDrafts((current) => ({ ...current, [row.row_id]: payload.wall_config ?? '' }));
      setFeedback((current) => ({ ...current, [row.row_id]: 'Saved for this countertop row.' }));
      setAttempt((count) => count + 1);
    } catch (caught) {
      setFeedback((current) => ({
        ...current,
        [row.row_id]: caught instanceof ApiError ? caught.message : String(caught),
      }));
    } finally {
      setSaving(null);
    }
  }

  if (error && rows.length === 0) {
    return (
      <section className="enter-values__section slot-reader-rows" aria-labelledby="slot-rows-title">
        <h2 id="slot-rows-title">Countertop rows read from the drawing</h2>
        <p className="enter-values__error" role="alert">
          The proposed rows could not be loaded: {error}{' '}
          <button type="button" onClick={() => setAttempt((count) => count + 1)}>Try again</button>
        </p>
      </section>
    );
  }
  if (rows.length === 0) return null;

  return (
    <section className="enter-values__section slot-reader-rows" aria-labelledby="slot-rows-title">
      <h2 id="slot-rows-title">Countertop rows read from the drawing</h2>
      <p className="enter-values__hint">
        Complete, unheld rows with walls found in the drawing can be checked automatically. A wall
        suggestion from the readers is only a proposal until you confirm it for this row. Missing
        widths can be entered here and stay attached to this row.
      </p>
      {rows.map((row) => {
        const selectedWall = rowWallSelection(wallDrafts[row.row_id], row.wall_config);
        const hasTypedDraft = Object.values(valueDrafts[row.row_id] ?? {}).some((value) => value.trim() !== '');
        const payload = slotReaderReviewPayload(row, wallDrafts[row.row_id], valueDrafts[row.row_id]);
        const canSave = !row.held_reason && saving !== row.row_id && (
          hasTypedDraft || payload.wall_config !== undefined
        );
        return (
          <article className="slot-reader-rows__row" key={row.row_id} id={`slot-row-${row.row_id}`} tabIndex={-1}>
            <header>
              <h3>Page {row.page_number}: {row.label}</h3>
              <span>{row.piece_count} pieces</span>
            </header>
            {row.held_reason && <p className="slot-reader-rows__hold" role="status">Needs review: {row.held_reason}</p>}
            {row.held_reason && onReviewRow && <button type="button" className="btn btn--subtle" onClick={() => onReviewRow(row.row_id)}>Record a decision in Results</button>}
            <ul className="slot-reader-rows__values">
              {row.values.map((item) => (
                <li key={item.key}>
                  {item.needs_value ? (
                    <label>
                      <span>{item.label}</span>
                      {item.suggestion && (
                        <small className="slot-reader-rows__reason">
                          Reader proposal (not yet confirmed): {item.suggestion}
                          {item.review_reason ? ` — ${item.review_reason}` : ''}
                        </small>
                      )}
                      <input
                        type="text"
                        inputMode="decimal"
                        placeholder="Enter the value with its unit"
                        value={valueDrafts[row.row_id]?.[item.key] ?? ''}
                        disabled={Boolean(row.held_reason) || saving === row.row_id}
                        onChange={(event) => setValueDrafts((current) => ({
                          ...current,
                          [row.row_id]: { ...current[row.row_id], [item.key]: event.target.value },
                        }))}
                      />
                    </label>
                  ) : (
                    <span>
                      <strong>{item.label}:</strong> {item.value} <small>({item.source})</small>
                      {item.review_reason && <small className="slot-reader-rows__reason">{item.review_reason}</small>}
                    </span>
                  )}
                </li>
              ))}
            </ul>
            {row.wall_source === 'vendor-drawing-clues' && row.wall_proposal && (
              <p className="slot-reader-rows__wall-note">
                Wall layout from drawing clues: {row.wall_proposal.replaceAll('_', ' ')}. This is the row&apos;s check input.
              </p>
            )}
            {shouldOfferRowWallControl(row) && (
              <label className="slot-reader-rows__wall">
                Wall layout for this row
                <select
                  value={selectedWall}
                  disabled={!row.wall_confirmation_allowed || saving === row.row_id}
                  onChange={(event) => setWallDrafts((current) => ({ ...current, [row.row_id]: event.target.value }))}
                >
                  <option value="">Choose this row&apos;s wall layout</option>
                  {row.wall_layout_choices.map((choice) => (
                    <option value={choice} key={choice}>
                      {row.wall_source === 'between-panels' && choice === 'back_only'
                        ? 'No field cut at the ends (back only)' : choice.replaceAll('_', ' ')}
                    </option>
                  ))}
                </select>
                {(row.wall_source === 'readers' || row.wall_source === 'drawing-and-readers') && row.wall_proposal && row.wall_config === null && (
                  <small>Suggested partly or fully by the readers. Choose it for this row before saving it.</small>
                )}
                {row.wall_reason && <small>{row.wall_reason}</small>}
              </label>
            )}
            {selectedWall && selectedWall !== row.wall_config && row.wall_confirmation_allowed && (
              <button
                type="button"
                className="btn btn--sm"
                disabled={!row.wall_confirmation_allowed || saving === row.row_id}
                onClick={() => void save(row, { wall_config: selectedWall })}
              >
                Use this wall layout for this row
              </button>
            )}
            {(row.wall_source === 'readers' || row.wall_source === 'drawing-and-readers' || row.wall_source === 'between-panels') &&
              row.wall_proposal && row.wall_config === null && !selectedWall && (
                <div className="slot-reader-rows__wall-note">
                <p>{row.wall_source === 'between-panels' ? "The stone sits between side panels, so the panels take the field cut. Proposed: no field cut at the ends (back only)." : `Reader proposal: ${row.wall_proposal.replaceAll('_', ' ')}. Not confirmed for this row.`}</p>
                <button
                  type="button"
                  className="btn btn--sm"
                  disabled={!row.wall_confirmation_allowed || saving === row.row_id}
                  onClick={() => void save(row, { wall_config: row.wall_proposal! })}
                >
                  Use this wall layout for this row
                </button>
                </div>
              )}
            <div className="slot-reader-rows__actions">
              <button type="button" className="btn btn--sm btn--primary" disabled={!canSave} onClick={() => void save(row)}>
                {saving === row.row_id ? 'Saving…' : 'Save this row'}
              </button>
              {row.confirmed_by && <small>Last saved by {row.confirmed_by}</small>}
              {feedback[row.row_id] && <small role="status">{feedback[row.row_id]}</small>}
            </div>
          </article>
        );
      })}
    </section>
  );
}

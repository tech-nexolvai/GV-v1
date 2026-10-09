import { useEffect, useState } from 'react';

import {
  ApiError,
  getCountertopResults,
  listSlotReaderRows,
  reviewSlotReaderRow,
  type CountertopResult,
  type SlotReaderRow,
} from '../../api/client';
import { CountertopStrip } from '@/components/results/CountertopStrip';
import { projectId } from '../../api/config';
import { rowWallSelection, shouldOfferRowWallControl, slotReaderReviewPayload, slotRowCount, unsavedRowCount, type SlotReaderReviewPayload } from './slotReaderReview.js';
import type { StepCount } from '../../lib/measure-steps';
import { wallWords } from '@/lib/needs-you-queue';
import { InfoTip } from '@/components/measure/info-tip';
import { WallLayoutPicture } from '@/components/results/wall-glyph';
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group';
import './SlotReaderRows.css';

/** One reviewer decision per slot-reader row; no value or wall choice is revision-wide. */
export function SlotReaderRows({ packageId, refresh, targetRow, onTargetReached, onReviewRow, onOpenQueue, onProgress, onUnsaved }: { packageId: string; refresh: number; targetRow?: string | null; onTargetReached?: () => void; onReviewRow?: (rowId: string) => void; /** Opens the "Needs you" queue (#1050), where a held row is decided. */ onOpenQueue?: () => void; /** Its count for the Measurements step bar (#1061). */ onProgress?: (count: StepCount | null) => void; /** How many rows hold changes "Save this row" has not sent (#1061). */ onUnsaved?: (count: number) => void }) {
  const [rows, setRows] = useState<SlotReaderRow[]>([]);
  const [wallDrafts, setWallDrafts] = useState<Record<string, string>>({});
  const [valueDrafts, setValueDrafts] = useState<Record<string, Record<string, string>>>({});
  const [saving, setSaving] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  // Nothing is counted before the first answer, or after a failed one: "nothing to do" must be earned.
  const [loaded, setLoaded] = useState(false);
  const [attempt, setAttempt] = useState(0);
  // The countertop picture for each row (#1043), from the same countertop-results the dashboard
  // reads. Optional: if it cannot be loaded, the card keeps its text list of widths.
  const [pictures, setPictures] = useState<Map<string, CountertopResult>>(new Map());
  useEffect(() => {
    onProgress?.(loaded && !error ? slotRowCount(rows) : null);
  }, [rows, loaded, error, onProgress]);
  useEffect(() => {
    onUnsaved?.(unsavedRowCount(rows, wallDrafts, valueDrafts));
  }, [rows, wallDrafts, valueDrafts, onUnsaved]);

  useEffect(() => {
    let live = true;
    listSlotReaderRows(projectId(), packageId)
      .then((result) => {
        if (live) {
          setRows(result.rows);
          setError(null);
          setLoaded(true);
        }
      })
      .catch((caught: unknown) => {
        if (live) setError(caught instanceof ApiError ? caught.message : String(caught));
      });
    getCountertopResults(projectId(), packageId)
      .then((result) => { if (live) setPictures(new Map(result.items.map((item) => [item.row_id, item]))); })
      .catch(() => { if (live) setPictures(new Map()); });
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
        Fill missing widths and confirm each row&apos;s walls.{' '}
        <InfoTip label="About countertop rows">
          <p>Complete, unheld rows with walls found in the drawing can be checked automatically.</p>
          <p>A wall suggestion from the readers is only a proposal until you confirm it for this row. Missing widths can be entered here and stay attached to this row.</p>
        </InfoTip>
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
            {pictures.has(row.row_id) && <CountertopStrip row={pictures.get(row.row_id)!} size="full" showHoldReason={false} className="slot-reader-rows__picture" />}
            {/* Held: the whole reason, as before, and where it is decided (#1061: the queue too). */}
            {row.held_reason && <p className="slot-reader-rows__hold" role="status">Needs review: {row.held_reason}</p>}
            {row.held_reason && (onOpenQueue || onReviewRow) && (
              <div className="flex flex-wrap items-center gap-2">
                {onOpenQueue && <button type="button" className="btn btn--sm" onClick={onOpenQueue}>Decide in the queue</button>}
                {onReviewRow && <button type="button" className="btn btn--subtle btn--sm" onClick={() => onReviewRow(row.row_id)}>Record a decision in Results</button>}
              </div>
            )}
            <ul className="slot-reader-rows__values">
              {/* With the picture shown, the read-only widths are in it; only the inputs stay listed. */}
              {row.values.filter((item) => item.needs_value || !pictures.has(row.row_id)).map((item) => (
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
            {/* The wall choice as picture buttons (#1061). Choosing is only a draft: the saved answer is
                selected, a reader proposal never is, and nothing is sent until "Use this wall layout". */}
            {shouldOfferRowWallControl(row) && (
              <div className="slot-reader-rows__wall flex flex-col gap-1.5 font-sans" data-tw>
                <span className="text-sm font-medium" id={`slot-walls-${row.row_id}`}>Wall layout for this row</span>
                <ToggleGroup
                  type="single"
                  variant="outline"
                  size="sm"
                  value={selectedWall}
                  disabled={!row.wall_confirmation_allowed || saving === row.row_id}
                  onValueChange={(next) => setWallDrafts((current) => ({ ...current, [row.row_id]: next }))}
                  aria-labelledby={`slot-walls-${row.row_id}`}
                  className="flex-wrap justify-start"
                >
                  {row.wall_layout_choices.map((choice) => (
                    <ToggleGroupItem key={choice} value={choice} className="gap-1.5 px-2.5">
                      <WallLayoutPicture config={choice} />
                      {row.wall_source === 'between-panels' && choice === 'back_only' ? 'No field cut at the ends (back only)' : wallWords(choice)}
                    </ToggleGroupItem>
                  ))}
                </ToggleGroup>
                {(row.wall_source === 'readers' || row.wall_source === 'drawing-and-readers') && row.wall_proposal && row.wall_config === null && (
                  <small className="text-xs text-muted-foreground">Suggested partly or fully by the readers. Choose it for this row before saving it.</small>
                )}
                {row.wall_reason && <small className="text-xs text-muted-foreground">{row.wall_reason}</small>}
              </div>
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

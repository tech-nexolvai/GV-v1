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
import { ArchitectLine } from '@/components/results/architect-line';
import { projectId } from '../../api/config';
import { rowWallSelection, shouldOfferRowWallControl, slotReaderReviewPayload, slotRowCount, unsavedRowCount, type SlotReaderReviewPayload } from './slotReaderReview.js';
import type { SectionState, StepCount } from '../../lib/measure-steps';
import { wallWords } from '@/lib/needs-you-queue';
import { WallLayoutPicture } from '@/components/results/wall-glyph';
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group';
import { Button } from '@/components/ui/button';
import { Caution, Hint, INPUT_CLASS, LoadError, StepSection } from './wizard-ui.js';

/** One reviewer decision per slot-reader row; no value or wall choice is revision-wide. */
export function SlotReaderRows({ packageId, refresh, targetRow, onTargetReached, onReviewRow, onOpenQueue, onProgress, onUnsaved, onState }: { packageId: string; refresh: number; targetRow?: string | null; onTargetReached?: () => void; onReviewRow?: (rowId: string) => void; /** Opens the "Needs you" queue (#1050), where a held row is decided. */ onOpenQueue?: () => void; /** Its count for the Measurements step bar (#1061). */ onProgress?: (count: StepCount | null) => void; /** How many rows hold changes "Save this row" has not sent (#1061). */ onUnsaved?: (count: number) => void; /** For the step's "nothing here" line (#1124). */ onState?: (state: SectionState) => void }) {
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
    onState?.(error ? 'error' : !loaded ? 'loading' : rows.length === 0 ? 'empty' : 'shown');
  }, [rows, loaded, error, onState]);
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

  const tip = (
    <>
      <p>Complete, unheld rows with walls found in the drawing can be checked automatically.</p>
      <p>A wall suggestion from the readers is only a proposal until you confirm it for this row. Missing widths can be entered here and stay attached to this row.</p>
    </>
  );
  if (error && rows.length === 0) {
    return (
      <StepSection id="slot-rows-title" slot="slot-reader-rows" title="Countertop rows read from the drawing">
        <LoadError onRetry={() => setAttempt((count) => count + 1)}>The proposed rows could not be loaded: {error}</LoadError>
      </StepSection>
    );
  }
  if (rows.length === 0) return null;

  const count = slotRowCount(rows);
  return (
    <StepSection
      id="slot-rows-title"
      slot="slot-reader-rows"
      title="Countertop rows read from the drawing"
      line={
        <strong className="font-medium text-foreground">
          {count.done === count.total
            ? `All ${count.total} countertop ${count.total === 1 ? 'row' : 'rows'} done.`
            : `${count.done} of ${count.total} countertop rows done.`}
        </strong>
      }
      tipLabel="About countertop rows"
      tip={tip}
    >
      {rows.map((row) => {
        const selectedWall = rowWallSelection(wallDrafts[row.row_id], row.wall_config);
        const hasTypedDraft = Object.values(valueDrafts[row.row_id] ?? {}).some((value) => value.trim() !== '');
        const payload = slotReaderReviewPayload(row, wallDrafts[row.row_id], valueDrafts[row.row_id]);
        const canSave = !row.held_reason && saving !== row.row_id && (
          hasTypedDraft || payload.wall_config !== undefined
        );
        const picture = pictures.get(row.row_id);
        return (
          <article
            data-slot="slot-reader-row"
            className="flex min-w-0 scroll-mt-4 flex-col gap-4 rounded-xl border bg-card p-4 outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50"
            key={row.row_id}
            id={`slot-row-${row.row_id}`}
            tabIndex={-1}
          >
            <header className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
              <h4 className="text-sm font-medium">
                Page <span className="num">{row.page_number}</span>: {row.label}
              </h4>
              <span className="text-xs text-muted-foreground">
                <span className="num">{row.piece_count}</span> {row.piece_count === 1 ? 'piece' : 'pieces'}
              </span>
            </header>
            {/* The countertop picture first (#1043): numbers and pictures before words. */}
            {picture && <CountertopStrip row={picture} size="full" showHoldReason={false} className="min-w-0" />}
            {/* Whether it matches the architect (#1085): the same line as in Results. */}
            {picture && <ArchitectLine result={picture.architect} />}
            {/* Held: the whole reason, as before, and where it is decided (#1061: the queue too). */}
            {row.held_reason && <Caution className="text-sm">Needs review: {row.held_reason}</Caution>}
            {row.held_reason && (onOpenQueue || onReviewRow) && (
              <div className="flex flex-wrap items-center gap-2">
                {onOpenQueue && <Button type="button" size="sm" variant="outline" onClick={onOpenQueue}>Decide in the queue</Button>}
                {onReviewRow && <Button type="button" size="sm" variant="ghost" onClick={() => onReviewRow(row.row_id)}>Record a decision in Results</Button>}
              </div>
            )}
            {/* With the picture shown, the read-only widths are in it; only the inputs stay listed. */}
            {row.values.some((item) => item.needs_value || !picture) && (
              <ul className="flex flex-col gap-3">
                {row.values.filter((item) => item.needs_value || !picture).map((item) => (
                  <li key={item.key}>
                    {item.needs_value ? (
                      <label className="flex max-w-md flex-col gap-1">
                        <span className="text-sm">{item.label}</span>
                        {item.suggestion && (
                          <Hint>
                            Reader proposal (not yet confirmed): <span className="num">{item.suggestion}</span>
                            {item.review_reason ? ` — ${item.review_reason}` : ''}
                          </Hint>
                        )}
                        <input
                          type="text"
                          inputMode="decimal"
                          className={`${INPUT_CLASS} num`}
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
                      <div className="text-sm">
                        <span className="font-medium">{item.label}:</span> <span className="num">{item.value}</span>{' '}
                        <span className="text-xs text-muted-foreground">({item.source})</span>
                        {item.review_reason && <Hint>{item.review_reason}</Hint>}
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            )}
            {row.wall_source === 'vendor-drawing-clues' && row.wall_proposal && (
              <Hint>
                Wall layout from drawing clues: {row.wall_proposal.replaceAll('_', ' ')}. This is the row&apos;s check input.
              </Hint>
            )}
            {/* The wall choice as picture buttons (#1061). Choosing is only a draft: the saved answer is
                selected, a reader proposal never is, and nothing is sent until "Use this wall layout". */}
            {shouldOfferRowWallControl(row) && (
              <div className="flex flex-col gap-1.5">
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
                  <Hint>Suggested partly or fully by the readers. Choose it for this row before saving it.</Hint>
                )}
                {row.wall_reason && <Hint>{row.wall_reason}</Hint>}
              </div>
            )}
            {selectedWall && selectedWall !== row.wall_config && row.wall_confirmation_allowed && (
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="self-start"
                disabled={!row.wall_confirmation_allowed || saving === row.row_id}
                onClick={() => void save(row, { wall_config: selectedWall })}
              >
                Use this wall layout for this row
              </Button>
            )}
            {(row.wall_source === 'readers' || row.wall_source === 'drawing-and-readers' || row.wall_source === 'between-panels') &&
              row.wall_proposal && row.wall_config === null && !selectedWall && (
                <div className="flex flex-col items-start gap-2">
                  <Hint>{row.wall_source === 'between-panels' ? "The stone sits between side panels, so the panels take the field cut. Proposed: no field cut at the ends (back only)." : `Reader proposal: ${row.wall_proposal.replaceAll('_', ' ')}. Not confirmed for this row.`}</Hint>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={!row.wall_confirmation_allowed || saving === row.row_id}
                    onClick={() => void save(row, { wall_config: row.wall_proposal! })}
                  >
                    Use this wall layout for this row
                  </Button>
                </div>
              )}
            <div className="flex flex-wrap items-center gap-3 border-t pt-3">
              <Button type="button" size="sm" variant="secondary" disabled={!canSave} onClick={() => void save(row)}>
                {saving === row.row_id ? 'Saving…' : 'Save this row'}
              </Button>
              {row.confirmed_by && <span className="text-xs text-muted-foreground">Last saved by {row.confirmed_by}</span>}
              {feedback[row.row_id] && <span className="text-xs text-muted-foreground" role="status">{feedback[row.row_id]}</span>}
            </div>
          </article>
        );
      })}
    </StepSection>
  );
}

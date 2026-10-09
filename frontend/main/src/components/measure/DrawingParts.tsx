import { useCallback, useEffect, useState } from 'react';

import {
  ApiError,
  addDrawingPart,
  confirmDrawingPart,
  downloadPartCrop,
  downloadVendorPagePicture,
  listDrawingParts,
  prepareVendorPagePictures,
  withdrawDrawingPart,
} from '../../api/client';
import { projectId } from '../../api/config';
import { partCount, type PartDrawing, type PartKind, type SuggestedPart } from './drawingPartChoices.js';
import type { SectionState, StepCount } from '../../lib/measure-steps';
import { createDecisionSaver, type DecisionFeedback } from './decisionFeedback.js';
import { DrawingPartsList, type NewPart } from './DrawingPartsList.js';
import { PartPicture } from './PartPicture.js';
import { Button } from '@/components/ui/button';
import { Hint, LoadError } from './wizard-ui.js';

/**
 * Loads a package's suggested parts and saves a person's decision on each, one at a time (#882).
 *
 * Renders nothing while no drawing has a suggestion or is confirmed as the vendor's. `refresh` is
 * the page's own re-read counter: it changes while the drawings are still being read and after a
 * drawing's role is confirmed, which are the two moments the list can change without a decision
 * here. After each decision the list is read again, so positions and the count stay the server's,
 * and `onDecided` is told, because the runs under each countertop (#893) depend on the parts.
 */
export function DrawingParts({
  packageId,
  refresh,
  onDecided,
  onProgress,
  onState,
}: {
  packageId: string;
  refresh: number;
  /** Told after each decision is saved, so what depends on the parts can be read again. */
  onDecided?: () => void;
  /** Its count for the Measurements step bar (#1061): reported whenever the list changes. */
  onProgress?: (count: StepCount | null) => void;
  /** Loaded and empty, shown, loading or failed: for the step's "nothing here" line (#1124). */
  onState?: (state: SectionState) => void;
}) {
  const [drawings, setDrawings] = useState<PartDrawing[] | null>(null);
  const [feedback, setFeedback] = useState<Record<string, DecisionFeedback>>({});
  const [saveDecision] = useState(createDecisionSaver);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [decided, setDecided] = useState(0);
  const [picturePoll, setPicturePoll] = useState(0);
  useEffect(() => {
    if (drawings) onProgress?.(partCount(drawings));
  }, [drawings, onProgress]);
  useEffect(() => {
    onState?.(loadError ? 'error' : drawings === null ? 'loading' : drawings.length === 0 ? 'empty' : 'shown');
  }, [drawings, loadError, onState]);
  const loadPagePicture = useCallback(
    (viewId: string) => downloadVendorPagePicture(projectId(), packageId, viewId),
    [packageId],
  );

  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    listDrawingParts(projectId(), packageId)
      .then(async (result) => {
        if (!live) return;
        setDrawings(result.drawings);
        setLoadError(null);
        const missingVendorPictures = result.drawings.some(
          (drawing) => drawing.role === 'shop' && drawing.page_picture === null,
        );
        if (missingVendorPictures && picturePoll < 30) {
          if (picturePoll === 0) {
            await prepareVendorPagePictures(projectId(), packageId).catch((caught: unknown) => {
              if (live) setLoadError(caught instanceof ApiError ? caught.message : String(caught));
            });
          }
          timer = setTimeout(() => live && setPicturePoll((count) => count + 1), 1500);
        }
      })
      .catch((caught: unknown) => {
        if (live) setLoadError(caught instanceof ApiError ? caught.message : String(caught));
      });
    return () => {
      live = false;
      if (timer) clearTimeout(timer);
    };
  }, [packageId, refresh, decided, picturePoll, loadAttempt]);

  async function save(key: string, decide: () => Promise<unknown>) {
    await saveDecision(key, decide, (item, state) => {
      setFeedback((current) => ({ ...current, [item]: state }));
    }, () => {
      setDecided((count) => count + 1);
      onDecided?.();
    });
  }

  function confirm(part: SuggestedPart, kind: PartKind, code: string | null) {
    void save(part.proposal_id, () =>
      confirmDrawingPart(projectId(), packageId, part.proposal_id, kind, code),
    );
  }

  function withdraw(part: SuggestedPart) {
    void save(part.proposal_id, () => withdrawDrawingPart(projectId(), packageId, part.proposal_id));
  }

  function add(drawing: PartDrawing, part: NewPart) {
    void save(drawing.view_id, () => addDrawingPart(projectId(), packageId, drawing.view_id, part));
  }

  if (drawings === null || drawings.length === 0) {
    return loadError ? (
      <LoadError onRetry={() => setLoadAttempt((count) => count + 1)}>
        The parts of these drawings could not be listed: {loadError}
      </LoadError>
    ) : null;
  }
  return (
    <>
      <DrawingPartsList
        loadPagePicture={loadPagePicture}
        drawings={drawings}
        saving={null}
        feedback={feedback}
        renderPicture={(_drawing, part) => (
          <PartPicture
            packageId={packageId}
            proposalId={part.proposal_id}
            alt={`Part ${part.position} on page ${part.page_index + 1}, as the vendor drew it`}
          />
        )}
        renderCrop={(_drawing, part) => <PartCrop packageId={packageId} part={part} />}
        onConfirm={confirm}
        onWithdraw={withdraw}
        onAdd={add}
      />
      {loadError && (
        <LoadError onRetry={() => setLoadAttempt((count) => count + 1)}>
          The parts list could not refresh: {loadError}
        </LoadError>
      )}
    </>
  );
}

/** The stored crop of the reading one part's code came from, loaded when it is shown. */
function PartCrop({ packageId, part }: { packageId: string; part: SuggestedPart }) {
  const [state, setState] = useState<{ url?: string; error?: string }>({});
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let live = true;
    let url: string | undefined;
    void downloadPartCrop(projectId(), packageId, part.proposal_id).then(
      (blob) => {
        url = URL.createObjectURL(blob);
        if (live) setState({ url });
      },
      () => {
        if (live) setState({ error: "The crop of this part's code could not be loaded." });
      },
    );
    return () => {
      live = false;
      if (url) URL.revokeObjectURL(url);
    };
  }, [packageId, part.proposal_id, attempt]);

  if (state.error) {
    return (
      <Hint className="flex flex-wrap items-center gap-2">
        {state.error}
        <Button type="button" size="xs" variant="outline" onClick={() => { setState({}); setAttempt((count) => count + 1); }}>Retry code crop</Button>
      </Hint>
    );
  }
  if (!state.url) return <Hint>Loading the picture…</Hint>;
  return (
    <img
      className="max-h-16 w-fit max-w-full rounded-md border bg-white object-contain"
      src={state.url}
      alt={`Where the code is printed on part ${part.position}, page ${part.page_index + 1}`}
    />
  );
}

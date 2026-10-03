import { useEffect, useState } from 'react';

import {
  ApiError,
  addDrawingPart,
  confirmDrawingPart,
  downloadPartCrop,
  listDrawingParts,
  withdrawDrawingPart,
} from '../../api/client';
import { projectId } from '../../api/config';
import { type PartDrawing, type PartKind, type SuggestedPart } from './drawingPartChoices.js';
import { DrawingPartsList, type NewPart } from './DrawingPartsList.js';
import { PartPicture, PartsLoadState, type PartImageState } from './PartRecovery.js';
import { loadPassageImage } from './passageImage.js';
import { createMeasurementDecisionSaver as createPartDecisionSaver, type MeasurementDecisionState as PartDecisionState } from './measurementDecisionSave.js';
import './DrawingParts.css';

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
}: {
  packageId: string;
  refresh: number;
  /** Told after each decision is saved, so what depends on the parts can be read again. */
  onDecided?: () => void;
}) {
  const [drawings, setDrawings] = useState<PartDrawing[] | null>(null);
  const [decisions, setDecisions] = useState<Record<string, PartDecisionState>>({});
  const [saveDecision] = useState(createPartDecisionSaver);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [retry, setRetry] = useState(0);
  const [decided, setDecided] = useState(0);

  useEffect(() => {
    let live = true;
    listDrawingParts(projectId(), packageId)
      .then((result) => {
        if (live) {
          setDrawings(result.drawings);
          setLoadError(null);
          setLoading(false);
        }
      })
      .catch((caught: unknown) => {
        if (live) {
          setLoadError(caught instanceof ApiError ? caught.message : String(caught));
          setLoading(false);
        }
      });
    return () => {
      live = false;
    };
  }, [packageId, refresh, decided, retry]);

  function save(key: string, decide: () => Promise<unknown>) {
    return saveDecision(key, decide, {
      state: (id, state) => setDecisions((prior) => ({ ...prior, [id]: state })),
      saved: () => {
        setDecided((count) => count + 1);
        onDecided?.();
      },
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

  return (
    <>
      <PartsLoadState error={loadError} loading={loading} hasDrawings={!!drawings?.length}
        onRetry={() => { setLoading(true); setRetry((count) => count + 1); }} />
      {!!drawings?.length && <DrawingPartsList
        drawings={drawings}
        decisions={decisions}
        renderCrop={(_drawing, part) => <PartCrop packageId={packageId} part={part} />}
        onConfirm={confirm}
        onWithdraw={withdraw}
        onAdd={add}
      />}
    </>
  );
}

/** The stored picture of one part, loaded when it is shown. */
function PartCrop({ packageId, part }: { packageId: string; part: SuggestedPart }) {
  const [attempt, setAttempt] = useState(0);
  return <PartCropRequest key={`${packageId}:${part.proposal_id}:${attempt}`} packageId={packageId}
    part={part} onRetry={() => setAttempt((value) => value + 1)} />;
}

function PartCropRequest({ packageId, part, onRetry }: {
  packageId: string; part: SuggestedPart; onRetry: () => void;
}) {
  const [state, setState] = useState<PartImageState>(null);
  useEffect(() => loadPassageImage(
    () => downloadPartCrop(projectId(), packageId, part.proposal_id),
    (url) => setState({ url }), () => setState({ error: true }),
  ), [packageId, part.proposal_id]);
  return <PartPicture state={state} position={part.position} page={part.page_index + 1}
    onRetry={onRetry} onError={() => setState({ error: true })} />;
}

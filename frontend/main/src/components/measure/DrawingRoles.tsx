import { useEffect, useState } from 'react';

import { ApiError, confirmDrawingRole, listDrawingViews } from '../../api/client';
import { projectId } from '../../api/config';
import { roleCount, type DrawingRole, type DrawingView } from './drawingRoleChoices.js';
import type { StepCount } from '../../lib/measure-steps';
import { DrawingRolesList } from './DrawingRolesList.js';
import { createDecisionSaver, type DecisionFeedback } from './decisionFeedback.js';

/**
 * Loads a package's drawings and saves a reviewer's answer about each (#795).
 *
 * Renders nothing for a package with no drawing views — two separate PDFs, where the upload says
 * which is which. `onConfirmed` runs after each saved answer, so the page re-reads its readings: a
 * reading on the drawing just confirmed now has a side and can be offered for a field.
 */
export function DrawingRoles({
  packageId,
  onConfirmed,
  onProgress,
}: {
  packageId: string;
  onConfirmed: () => void;
  /** Its count for the Measurements step bar (#1061): reported whenever the list changes. */
  onProgress?: (count: StepCount | null) => void;
}) {
  const [views, setViews] = useState<DrawingView[] | null>(null);
  const [feedback, setFeedback] = useState<Record<string, DecisionFeedback>>({});
  const [saveDecision] = useState(createDecisionSaver);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  useEffect(() => {
    if (views) onProgress?.(roleCount(views));
  }, [views, onProgress]);

  // Loaded once per mount; the page mounts one per package (`key`), so a package switch starts empty
  // rather than showing the last package's drawings while the next one's load.
  useEffect(() => {
    let live = true;
    listDrawingViews(projectId(), packageId)
      .then((result) => {
        if (live) { setViews(result.views); setLoadError(null); }
      })
      .catch((caught: unknown) => {
        if (live) setLoadError(caught instanceof ApiError ? caught.message : String(caught));
      });
    return () => {
      live = false;
    };
  }, [packageId, loadAttempt]);

  async function choose(view: DrawingView, role: DrawingRole) {
    await saveDecision(view.view_id, async () => {
      const updated = await confirmDrawingRole(projectId(), packageId, view.view_id, role);
      setViews((current) =>
        current ? current.map((item) => (item.view_id === updated.view_id ? updated : item)) : current,
      );
    }, (item, state) => setFeedback((current) => ({ ...current, [item]: state })), onConfirmed);
  }

  if (views === null || views.length === 0) {
    return loadError ? (
      <p className="enter-values__error" role="alert">
        The drawings on these sheets could not be listed: {loadError}{' '}
        <button type="button" onClick={() => setLoadAttempt((count) => count + 1)}>Try again</button>
      </p>
    ) : null;
  }
  return (
    <>
      <DrawingRolesList views={views} saving={null} feedback={feedback} onChoose={(view, role) => void choose(view, role)} />
      {loadError && (
        <p className="enter-values__error" role="alert">
          The drawing roles could not refresh: {loadError}{' '}
          <button type="button" onClick={() => setLoadAttempt((count) => count + 1)}>Try again</button>
        </p>
      )}
    </>
  );
}

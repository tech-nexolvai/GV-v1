import { useEffect, useState } from 'react';

import { ApiError, confirmDrawingRole, listDrawingViews } from '../../api/client';
import { projectId } from '../../api/config';
import { type DrawingRole, type DrawingView } from './drawingRoleChoices.js';
import { DrawingRolesList } from './DrawingRolesList.js';
import { DrawingRolesLoadState } from './DrawingRolesFeedback.js';
import { createMeasurementDecisionSaver, type MeasurementDecisionState } from './measurementDecisionSave.js';

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
}: {
  packageId: string;
  onConfirmed: () => void;
}) {
  const [views, setViews] = useState<DrawingView[] | null>(null);
  const [decisions, setDecisions] = useState<Record<string, MeasurementDecisionState>>({});
  const [saveDecision] = useState(createMeasurementDecisionSaver);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const [loading, setLoading] = useState(true);

  // Loaded on mount or explicit retry; one per package (`key`), so a package switch starts empty
  // rather than showing the last package's drawings while the next one's load.
  useEffect(() => {
    let live = true;
    listDrawingViews(projectId(), packageId)
      .then((result) => {
        if (live) {
          setViews(result.views);
          setError(null);
          setLoading(false);
        }
      })
      .catch((caught: unknown) => {
        if (live) {
          setError(caught instanceof ApiError ? caught.message : String(caught));
          setLoading(false);
        }
      });
    return () => {
      live = false;
    };
  }, [packageId, retry]);

  async function choose(view: DrawingView, role: DrawingRole) {
    await saveDecision(view.view_id, async () => {
      const updated = await confirmDrawingRole(projectId(), packageId, view.view_id, role);
      setViews((current) =>
        current ? current.map((item) => (item.view_id === updated.view_id ? updated : item)) : current,
      );
    }, {
      state: (id, state) => setDecisions((prior) => ({ ...prior, [id]: state })),
      saved: onConfirmed,
    });
  }

  if (views === null || views.length === 0) {
    return <DrawingRolesLoadState error={error} loading={loading}
      onRetry={() => { setLoading(true); setRetry((count) => count + 1); }} />;
  }
  return (
    <DrawingRolesList views={views} decisions={decisions} onChoose={(view, role) => void choose(view, role)} />
  );
}

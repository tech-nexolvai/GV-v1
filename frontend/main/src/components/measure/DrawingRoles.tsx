import { useEffect, useState } from 'react';

import { ApiError, confirmDrawingRole, listDrawingViews } from '../../api/client';
import { projectId } from '../../api/config';
import { type DrawingRole, type DrawingView } from './drawingRoleChoices.js';
import { DrawingRolesList } from './DrawingRolesList.js';

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
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Loaded once per mount; the page mounts one per package (`key`), so a package switch starts empty
  // rather than showing the last package's drawings while the next one's load.
  useEffect(() => {
    let live = true;
    listDrawingViews(projectId(), packageId)
      .then((result) => {
        if (live) setViews(result.views);
      })
      .catch((caught: unknown) => {
        if (live) setError(caught instanceof ApiError ? caught.message : String(caught));
      });
    return () => {
      live = false;
    };
  }, [packageId]);

  async function choose(view: DrawingView, role: DrawingRole) {
    setSaving(view.view_id);
    setError(null);
    try {
      const updated = await confirmDrawingRole(projectId(), packageId, view.view_id, role);
      setViews((current) =>
        current ? current.map((item) => (item.view_id === updated.view_id ? updated : item)) : current,
      );
      onConfirmed();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setSaving(null);
    }
  }

  if (views === null || views.length === 0) {
    return error ? (
      <p className="enter-values__error" role="alert">
        The drawings on these sheets could not be listed: {error}
      </p>
    ) : null;
  }
  return (
    <>
      <DrawingRolesList views={views} saving={saving} onChoose={(view, role) => void choose(view, role)} />
      {error && (
        <p className="enter-values__error" role="alert">
          {error}
        </p>
      )}
    </>
  );
}

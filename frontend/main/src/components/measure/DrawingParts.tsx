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
import './DrawingParts.css';

/**
 * Loads a package's suggested parts and saves a person's decision on each, one at a time (#882).
 *
 * Renders nothing while no drawing has a suggestion or is confirmed as the vendor's. `refresh` is
 * the page's own re-read counter: it changes while the drawings are still being read and after a
 * drawing's role is confirmed, which are the two moments the list can change without a decision
 * here. After each decision the list is read again, so positions and the count stay the server's.
 */
export function DrawingParts({ packageId, refresh }: { packageId: string; refresh: number }) {
  const [drawings, setDrawings] = useState<PartDrawing[] | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [decided, setDecided] = useState(0);

  useEffect(() => {
    let live = true;
    listDrawingParts(projectId(), packageId)
      .then((result) => {
        if (live) setDrawings(result.drawings);
      })
      .catch((caught: unknown) => {
        if (live) setError(caught instanceof ApiError ? caught.message : String(caught));
      });
    return () => {
      live = false;
    };
  }, [packageId, refresh, decided]);

  async function save(key: string, decide: () => Promise<unknown>) {
    setSaving(key);
    setError(null);
    try {
      await decide();
      setDecided((count) => count + 1);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setSaving(null);
    }
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
    return error ? (
      <p className="enter-values__error" role="alert">
        The parts of these drawings could not be listed: {error}
      </p>
    ) : null;
  }
  return (
    <>
      <DrawingPartsList
        drawings={drawings}
        saving={saving}
        renderCrop={(_drawing, part) => <PartCrop packageId={packageId} part={part} />}
        onConfirm={confirm}
        onWithdraw={withdraw}
        onAdd={add}
      />
      {error && (
        <p className="enter-values__error" role="alert">
          {error}
        </p>
      )}
    </>
  );
}

/** The stored picture of one part, loaded when it is shown. */
function PartCrop({ packageId, part }: { packageId: string; part: SuggestedPart }) {
  const [state, setState] = useState<{ url?: string; error?: string }>({});

  useEffect(() => {
    let live = true;
    let url: string | undefined;
    void downloadPartCrop(projectId(), packageId, part.proposal_id).then(
      (blob) => {
        url = URL.createObjectURL(blob);
        if (live) setState({ url });
      },
      () => {
        if (live) setState({ error: 'The stored picture of this part could not be loaded.' });
      },
    );
    return () => {
      live = false;
      if (url) URL.revokeObjectURL(url);
    };
  }, [packageId, part.proposal_id]);

  if (state.error) return <p className="drawing-parts__no-picture">{state.error}</p>;
  if (!state.url) return <p className="drawing-parts__no-picture">Loading the picture…</p>;
  return (
    <img
      className="drawing-parts__crop"
      src={state.url}
      alt={`Where the code is printed on part ${part.position}, page ${part.page_index + 1}`}
    />
  );
}

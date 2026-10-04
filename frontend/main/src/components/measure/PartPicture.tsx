import { useEffect, useState } from 'react';

import { downloadPartPicture } from '../../api/client';
import { projectId } from '../../api/config';

/**
 * One part's own picture: the vendor's drawing round its outline, cut by the worker (#897).
 *
 * Loaded when it is shown, and checked by the server against its recorded digest first. If it cannot
 * be loaded the row says so rather than showing anything else. It links to itself at full size,
 * because a picture is shrunk to fit its row. For a person's eyes only: nothing reads a value from it.
 */
export function PartPicture({
  packageId,
  proposalId,
  alt,
}: {
  packageId: string;
  proposalId: string;
  alt: string;
}) {
  const [state, setState] = useState<{ url?: string; error?: string }>({});

  useEffect(() => {
    let live = true;
    let url: string | undefined;
    void downloadPartPicture(projectId(), packageId, proposalId).then(
      (blob) => {
        url = URL.createObjectURL(blob);
        if (live) setState({ url });
      },
      () => {
        if (live) setState({ error: 'The picture of this part could not be loaded.' });
      },
    );
    return () => {
      live = false;
      if (url) URL.revokeObjectURL(url);
    };
  }, [packageId, proposalId]);

  if (state.error) return <p className="drawing-parts__no-picture">{state.error}</p>;
  if (!state.url) return <p className="drawing-parts__no-picture">Loading the picture…</p>;
  return (
    <a href={state.url} target="_blank" rel="noreferrer" title="Open the picture at full size">
      <img className="drawing-parts__part-picture" src={state.url} alt={alt} />
    </a>
  );
}

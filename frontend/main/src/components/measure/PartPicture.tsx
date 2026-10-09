import { useEffect, useState } from 'react';

import { downloadPartPicture } from '../../api/client';
import { projectId } from '../../api/config';
import { Button } from '@/components/ui/button';
import { Hint } from './wizard-ui.js';

/** How a part's picture sits in its card: whole, on white, never taller than a phone's half screen. */
export const PICTURE_CLASS = 'max-h-56 w-full rounded-lg border bg-white object-contain';

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
  const [attempt, setAttempt] = useState(0);

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
  }, [packageId, proposalId, attempt]);

  if (state.error) {
    return (
      <Hint className="flex flex-wrap items-center gap-2">
        {state.error}
        <Button type="button" size="xs" variant="outline" onClick={() => { setState({}); setAttempt((count) => count + 1); }}>Retry picture</Button>
      </Hint>
    );
  }
  if (!state.url) return <div className="h-32 w-full animate-pulse rounded-lg bg-muted motion-reduce:animate-none" aria-label="Loading the picture…" role="status" />;
  return (
    <a href={state.url} target="_blank" rel="noreferrer" title="Open the picture at full size">
      <img className={PICTURE_CLASS} src={state.url} alt={alt} />
    </a>
  );
}

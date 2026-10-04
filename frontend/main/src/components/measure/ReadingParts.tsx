import { useEffect, useState } from 'react';

import { ApiError, confirmReadingPart, listReadingParts, withdrawReadingPart } from '../../api/client';
import { projectId } from '../../api/config';
import { PartPicture } from './PartPicture.js';
import { partName, type LinkPart, type ReadingLinks } from './readingPartChoices.js';
import { ReadingPartsList } from './ReadingPartsList.js';
import './DrawingParts.css';

/**
 * Loads each confirmed part's suggested reading and saves a person's decision on one part at a time
 * (#913).
 *
 * Renders nothing until a vendor drawing has a confirmed part. `refresh` is the page's re-read
 * counter, which also changes after a decision on a part above and after a reading is confirmed: both
 * change what can be suggested. After each decision here the list is read again, so the link shown
 * is always the server's.
 */
export function ReadingParts({ packageId, refresh }: { packageId: string; refresh: number }) {
  const [links, setLinks] = useState<ReadingLinks | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [decided, setDecided] = useState(0);

  useEffect(() => {
    let live = true;
    listReadingParts(projectId(), packageId)
      .then((result) => {
        if (live) setLinks(result);
      })
      .catch((caught: unknown) => {
        if (live) setError(caught instanceof ApiError ? caught.message : String(caught));
      });
    return () => {
      live = false;
    };
  }, [packageId, refresh, decided]);

  async function save(part: LinkPart, decide: () => Promise<unknown>) {
    setSaving(part.item_id);
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

  function confirm(part: LinkPart, readingId: string) {
    void save(part, () => confirmReadingPart(projectId(), packageId, part.item_id, readingId));
  }

  function withdraw(part: LinkPart) {
    void save(part, () => withdrawReadingPart(projectId(), packageId, part.item_id));
  }

  if (links === null || links.drawings.length === 0) {
    return error ? (
      <p className="enter-values__error" role="alert">
        Which reading is each part&apos;s width could not be listed: {error}
      </p>
    ) : null;
  }
  return (
    <>
      <ReadingPartsList
        links={links}
        saving={saving}
        renderPicture={(drawing, part) =>
          part.proposal_id ? (
            <PartPicture
              packageId={packageId}
              proposalId={part.proposal_id}
              alt={`${partName(part)} on page ${drawing.page_index + 1}, as the vendor drew it`}
            />
          ) : null
        }
        onConfirm={confirm}
        onWithdraw={withdraw}
      />
      {error && (
        <p className="enter-values__error" role="alert">
          {error}
        </p>
      )}
    </>
  );
}

/**
 * What the depth and sink reader read for each countertop (#1295), on a waiting depth or sink check.
 *
 * One line per value: what was read and how code checked it, with the reading's own picture; a value
 * only its drawn length checks, with **Confirm this reading** (the same confirm path Measurements
 * uses: a person confirms the number that was read, never types one); or why a value was not used.
 * Nothing here decides a check: a confirmed reading is used once the checks run again.
 */
import { useEffect, useState } from 'react';

import { confirmCandidate, downloadCandidateCrop } from '@/api/client';
import { projectId } from '@/api/config';
import { Button } from '@/components/ui/button';
import { candidateCropWarning } from '@/components/measure/candidateCropWarning';
import { byCountertop, readingSentence, type DepthReading } from '@/lib/depth-readings';

export function DepthReadings({
  readings,
  packageId,
  canAct,
  onConfirmed,
  onRunChecks,
}: {
  readings: readonly DepthReading[];
  packageId: string;
  /** False on a closed review: the lines show, the Confirm action does not. */
  canAct: boolean;
  /** A reading was confirmed: the page reloads the results. */
  onConfirmed: () => void;
  onRunChecks?: () => void;
}) {
  const [confirming, setConfirming] = useState<string | null>(null);
  const [confirmedHere, setConfirmedHere] = useState<ReadonlySet<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  if (readings.length === 0) return null;

  async function confirm(reading: DepthReading) {
    if (!reading.candidate_id || confirming) return;
    setConfirming(reading.candidate_id);
    setError(null);
    try {
      await confirmCandidate(projectId(), packageId, reading.candidate_id, reading.field);
      setConfirmedHere((prior) => new Set(prior).add(reading.candidate_id as string));
      onConfirmed();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setConfirming(null);
    }
  }

  return (
    <div data-slot="depth-readings" className="flex flex-col gap-3">
      {byCountertop(readings).map(([countertop, lines]) => (
        <section key={countertop} aria-label={`What was read for ${countertop}`} className="flex flex-col gap-2">
          <p className="text-xs font-medium text-muted-foreground">{countertop}</p>
          <ul className="flex flex-col gap-2.5">
            {lines.map((reading, index) => {
              const done = reading.candidate_id !== null && confirmedHere.has(reading.candidate_id);
              return (
                <li
                  key={`${reading.field}-${reading.candidate_id ?? index}`}
                  data-state={done ? 'confirmed' : reading.state}
                  data-field={reading.field}
                  className="flex flex-col gap-1.5"
                >
                  <p>
                    <span className="font-medium">{reading.label}:</span> {readingSentence(reading)}
                  </p>
                  {reading.has_picture && reading.candidate_id && (
                    <ReadingPicture packageId={packageId} reading={reading} />
                  )}
                  {reading.state === 'to_confirm' && !done && canAct && reading.can_confirm && (
                    <div>
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={confirming !== null}
                        onClick={() => void confirm(reading)}
                      >
                        {confirming === reading.candidate_id ? 'Confirming…' : 'Confirm this reading'}
                      </Button>
                    </div>
                  )}
                  {reading.state === 'to_confirm' && !done && canAct && !reading.can_confirm && (
                    <p className="text-xs text-muted-foreground">
                      Its picture is not ready yet, so it cannot be confirmed here. It can be confirmed on Measurements once
                      its picture is cut.
                    </p>
                  )}
                  {done && (
                    <p role="status" className="flex flex-wrap items-center gap-2 text-sm">
                      <span>Confirmed. Run checks to use it.</span>
                      {onRunChecks && (
                        <Button type="button" size="sm" variant="outline" onClick={onRunChecks}>
                          Run checks
                        </Button>
                      )}
                    </p>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      ))}
      {error && <p role="alert" className="text-outcome-fail-fg">{error}</p>}
    </div>
  );
}

/** The reading's own picture, as the evidence stage cut it, with GV's marks warned about. */
function ReadingPicture({ packageId, reading }: { packageId: string; reading: DepthReading }) {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const candidateId = reading.candidate_id;
  useEffect(() => {
    if (!candidateId) return;
    let live = true;
    let objectUrl: string | null = null;
    downloadCandidateCrop(projectId(), packageId, candidateId).then(
      (blob) => {
        if (typeof URL.createObjectURL !== 'function') return;
        objectUrl = URL.createObjectURL(blob);
        if (live) setUrl(objectUrl);
        else URL.revokeObjectURL(objectUrl);
      },
      () => {
        if (live) setFailed(true);
      },
    );
    return () => {
      live = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [candidateId, packageId]);
  if (failed) return <p className="text-xs text-muted-foreground">Its picture could not be loaded.</p>;
  if (!url) return <p className="text-xs text-muted-foreground">Loading its picture…</p>;
  const warning = candidateCropWarning(reading.picture_shows_gv_mark);
  return (
    <figure className="flex flex-col gap-1">
      <img
        className="max-h-40 w-fit max-w-full rounded-md border bg-white object-contain"
        src={url}
        alt={`Picture of the ${reading.label.toLowerCase()} read on page ${reading.page_number ?? ''}`}
      />
      {warning && <figcaption className="text-xs text-muted-foreground">{warning}</figcaption>}
    </figure>
  );
}

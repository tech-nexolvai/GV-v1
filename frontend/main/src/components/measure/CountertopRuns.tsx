import { useEffect, useState } from 'react';

import {
  ApiError,
  confirmCountertopRun,
  listCountertopRuns,
  withdrawCountertopRun,
} from '../../api/client';
import { projectId } from '../../api/config';
import { type RunCountertop, type RunsList } from './countertopRunChoices.js';
import { createDecisionSaver, type DecisionFeedback } from './decisionFeedback.js';
import { CountertopRunsList } from './CountertopRunsList.js';
import './DrawingParts.css';

/**
 * Loads each confirmed countertop's suggested run and saves a person's decision on one countertop
 * at a time (#893).
 *
 * Renders nothing until a vendor drawing has a confirmed part. `refresh` is the page's re-read
 * counter, which also changes after every decision on a part above: a part confirmed, corrected or
 * taken back changes what can be suggested. After each decision here the list is read again, so
 * the run shown is always the server's, in the server's order.
 */
export function CountertopRuns({ packageId, refresh }: { packageId: string; refresh: number }) {
  const [runs, setRuns] = useState<RunsList | null>(null);
  const [feedback, setFeedback] = useState<Record<string, DecisionFeedback>>({});
  const [saveDecision] = useState(createDecisionSaver);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [decided, setDecided] = useState(0);

  useEffect(() => {
    let live = true;
    listCountertopRuns(projectId(), packageId)
      .then((result) => {
        if (live) { setRuns(result); setLoadError(null); }
      })
      .catch((caught: unknown) => {
        if (live) setLoadError(caught instanceof ApiError ? caught.message : String(caught));
      });
    return () => {
      live = false;
    };
  }, [packageId, refresh, decided, loadAttempt]);

  async function save(countertop: RunCountertop, decide: () => Promise<unknown>) {
    await saveDecision(countertop.countertop_item_id, decide, (item, state) => {
      setFeedback((current) => ({ ...current, [item]: state }));
    }, () => setDecided((count) => count + 1));
  }

  function confirm(countertop: RunCountertop, partIds: string[], wallConfig: string) {
    void save(countertop, () =>
      confirmCountertopRun(projectId(), packageId, countertop.countertop_item_id, partIds, wallConfig),
    );
  }

  function withdraw(countertop: RunCountertop) {
    void save(countertop, () =>
      withdrawCountertopRun(projectId(), packageId, countertop.countertop_item_id),
    );
  }

  if (runs === null || runs.drawings.length === 0) {
    return loadError ? (
      <p className="enter-values__error" role="alert">
        The runs under each countertop could not be listed: {loadError}{' '}
        <button type="button" onClick={() => setLoadAttempt((count) => count + 1)}>Try again</button>
      </p>
    ) : null;
  }
  return (
    <>
      <CountertopRunsList runs={runs} saving={null} feedback={feedback} onConfirm={confirm} onWithdraw={withdraw} />
      {loadError && (
        <p className="enter-values__error" role="alert">
          The countertop runs could not refresh: {loadError}{' '}
          <button type="button" onClick={() => setLoadAttempt((count) => count + 1)}>Try again</button>
        </p>
      )}
    </>
  );
}

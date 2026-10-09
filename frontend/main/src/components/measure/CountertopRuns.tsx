import { useEffect, useState } from 'react';

import {
  ApiError,
  confirmCountertopRun,
  listCountertopRuns,
  withdrawCountertopRun,
} from '../../api/client';
import { projectId } from '../../api/config';
import { runCount, type RunCountertop, type RunsList } from './countertopRunChoices.js';
import type { SectionState, StepCount } from '../../lib/measure-steps';
import { createDecisionSaver, type DecisionFeedback } from './decisionFeedback.js';
import { CountertopRunsList } from './CountertopRunsList.js';
import { LoadError } from './wizard-ui.js';

/**
 * Loads each confirmed countertop's suggested run and saves a person's decision on one countertop
 * at a time (#893).
 *
 * Renders nothing until a vendor drawing has a confirmed part. `refresh` is the page's re-read
 * counter, which also changes after every decision on a part above: a part confirmed, corrected or
 * taken back changes what can be suggested. After each decision here the list is read again, so
 * the run shown is always the server's, in the server's order.
 */
export function CountertopRuns({ packageId, refresh, onProgress, onState }: { packageId: string; refresh: number; /** Its count for the Measurements step bar (#1061). */ onProgress?: (count: StepCount | null) => void; /** For the step's "nothing here" line (#1124). */ onState?: (state: SectionState) => void }) {
  const [runs, setRuns] = useState<RunsList | null>(null);
  const [feedback, setFeedback] = useState<Record<string, DecisionFeedback>>({});
  const [saveDecision] = useState(createDecisionSaver);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [decided, setDecided] = useState(0);
  useEffect(() => {
    if (runs) onProgress?.(runCount(runs));
  }, [runs, onProgress]);
  useEffect(() => {
    onState?.(loadError ? 'error' : runs === null ? 'loading' : runs.drawings.length === 0 ? 'empty' : 'shown');
  }, [runs, loadError, onState]);

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
      <LoadError onRetry={() => setLoadAttempt((count) => count + 1)}>
        The runs under each countertop could not be listed: {loadError}
      </LoadError>
    ) : null;
  }
  return (
    <>
      <CountertopRunsList runs={runs} saving={null} feedback={feedback} onConfirm={confirm} onWithdraw={withdraw} />
      {loadError && (
        <LoadError onRetry={() => setLoadAttempt((count) => count + 1)}>
          The countertop runs could not refresh: {loadError}
        </LoadError>
      )}
    </>
  );
}

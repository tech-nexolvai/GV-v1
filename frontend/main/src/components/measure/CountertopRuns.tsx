import { useEffect, useState } from 'react';

import {
  ApiError,
  confirmCountertopRun,
  listCountertopRuns,
  withdrawCountertopRun,
} from '../../api/client';
import { projectId } from '../../api/config';
import { type RunCountertop, type RunsList } from './countertopRunChoices.js';
import { CountertopRunsList } from './CountertopRunsList.js';
import { CountertopRunsLoadState } from './CountertopRunFeedback.js';
import { createMeasurementDecisionSaver, type MeasurementDecisionState } from './measurementDecisionSave.js';
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
  const [decisions, setDecisions] = useState<Record<string, MeasurementDecisionState>>({});
  const [saveDecision] = useState(createMeasurementDecisionSaver);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [retry, setRetry] = useState(0);
  const [decided, setDecided] = useState(0);

  useEffect(() => {
    let live = true;
    listCountertopRuns(projectId(), packageId)
      .then((result) => {
        if (live) {
          setRuns(result);
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
  }, [packageId, refresh, decided, retry]);

  function save(countertop: RunCountertop, decide: () => Promise<unknown>) {
    return saveDecision(countertop.countertop_item_id, decide, {
      state: (id, state) => setDecisions((prior) => ({ ...prior, [id]: state })),
      saved: () => setDecided((count) => count + 1),
    });
  }

  function confirm(countertop: RunCountertop, partIds: string[]) {
    void save(countertop, () =>
      confirmCountertopRun(projectId(), packageId, countertop.countertop_item_id, partIds),
    );
  }

  function withdraw(countertop: RunCountertop) {
    void save(countertop, () =>
      withdrawCountertopRun(projectId(), packageId, countertop.countertop_item_id),
    );
  }

  return (
    <>
      <CountertopRunsLoadState error={error} loading={loading} hasDrawings={!!runs?.drawings.length}
        onRetry={() => { setLoading(true); setRetry((value) => value + 1); }} />
      {!!runs?.drawings.length && <CountertopRunsList runs={runs} decisions={decisions}
        locked={!!error} onConfirm={confirm} onWithdraw={withdraw} />}
    </>
  );
}

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
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [decided, setDecided] = useState(0);

  useEffect(() => {
    let live = true;
    listCountertopRuns(projectId(), packageId)
      .then((result) => {
        if (live) setRuns(result);
      })
      .catch((caught: unknown) => {
        if (live) setError(caught instanceof ApiError ? caught.message : String(caught));
      });
    return () => {
      live = false;
    };
  }, [packageId, refresh, decided]);

  async function save(countertop: RunCountertop, decide: () => Promise<unknown>) {
    setSaving(countertop.countertop_item_id);
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

  if (runs === null || runs.drawings.length === 0) {
    return error ? (
      <p className="enter-values__error" role="alert">
        The runs under each countertop could not be listed: {error}
      </p>
    ) : null;
  }
  return (
    <>
      <CountertopRunsList runs={runs} saving={saving} onConfirm={confirm} onWithdraw={withdraw} />
      {error && (
        <p className="enter-values__error" role="alert">
          {error}
        </p>
      )}
    </>
  );
}

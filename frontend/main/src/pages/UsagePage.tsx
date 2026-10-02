/**
 * What this project has actually done, counted from the API.
 *
 * The previous version of this page was a fixed table, and one of its numbers was
 * **"False-PASS rate 0.0%"** — the primary safety metric of the whole system, shown as perfect with
 * nothing behind it. A critical false PASS is a wrong dimension that a reviewer was told was right;
 * claiming a rate of zero without measuring one is the single most misleading thing this UI could
 * say. It also listed an audit trail attributing actions and timestamps to a named person who never
 * performed them.
 *
 * So everything here is either counted from the API or is marked as not yet measured. There is no
 * usage endpoint — the totals are aggregated from `packages` and each package's `findings/summary`,
 * which is why the page says how many packages it looked at.
 */

import { useState } from 'react';
import { listPackages, getFindingCounts } from '../api/client';
import type { FindingCounts } from '../api/client';
import { projectId } from '../api/config';
import { OUTCOME_LABELS } from '../data/outcomeLabels.js';
import { useAsync } from '../api/useAsync';
import { PageFrame, PageLoadError } from '../components/ui/PageFrame';
import '../components/ui/PageFrame.css';
import './UsagePage.css';

/** How many packages to aggregate. Named, and reported on screen, rather than a silent truncation. */
const SAMPLE_LIMIT = 100;

interface Totals {
  packages: number;
  aggregated: number;
  counts: FindingCounts;
}

const ZERO: FindingCounts = {
  total: 0,
  passed: 0,
  failed: 0,
  review_required: 0,
  not_found: 0,
  no_applicable_rule: 0,
  critical_failed: 0,
};

async function loadTotals(): Promise<Totals> {
  const project = projectId();
  const page = await listPackages(project, { limit: SAMPLE_LIMIT });

  // Sequential rather than a burst of parallel requests: this is a summary screen and nothing here
  // is worth making the API absorb a hundred simultaneous queries for.
  const counts = { ...ZERO };
  for (const pkg of page.items) {
    const summary = await getFindingCounts(project, pkg.id);
    for (const key of Object.keys(counts) as (keyof FindingCounts)[]) {
      counts[key] += summary[key];
    }
  }

  return { packages: page.items.length, aggregated: page.items.length, counts };
}

export function UsagePage() {
  const [attempt, setAttempt] = useState(0);
  const totals = useAsync(loadTotals, [attempt]);

  return (
    <PageFrame title="Usage" className="usage-page" description="Counted from this project's packages and findings.">

      <div className="usage-page__body">
        {totals.status === 'loading' && <p className="page-frame__state" role="status">Counting…</p>}

        {/* Failure and emptiness must not look alike. Zeroes on a screen that could not reach the
            server would read as "nothing has gone wrong", which is a claim nobody made. */}
        {totals.status === 'error' && (
          <PageLoadError title="These figures could not be loaded" message={`${totals.error.message} These figures are unavailable; this is not a report of zero activity.`} onRetry={() => setAttempt((value) => value + 1)} />
        )}

        {totals.status === 'ready' && (
          <>
            <div className="usage-stats">
              <StatCard
                value={String(totals.data.packages)}
                label="Packages"
                sub={
                  totals.data.packages === SAMPLE_LIMIT
                    ? `first ${SAMPLE_LIMIT} — there may be more`
                    : 'in this project'
                }
              />
              <StatCard
                value={String(totals.data.counts.total)}
                label="Findings"
                sub={`across ${totals.data.aggregated} package${totals.data.aggregated === 1 ? '' : 's'}`}
              />
              <StatCard
                value={String(totals.data.counts.critical_failed)}
                label="Critical failures"
                sub="a critical check that did not pass"
              />
              {/* Not a number, on purpose. A false PASS is only knowable by comparing a verdict with
                  a known-correct answer, which is what the gold set is for — and that runs in the
                  eval harness, not here. A figure on this screen would be a guess wearing a metric's
                  clothes. */}
              <StatCard
                value="—"
                label="Critical false-PASS rate"
                sub="measured against the gold set, not from this screen"
              />
            </div>

            <div className="usage-section">
              <h2 className="usage-section__title">Finding outcomes</h2>
              {totals.data.counts.total === 0 ? (
                /* Scoped to what was counted. `loadTotals` stops at `SAMPLE_LIMIT` and never asks
                   for later pages, so "nothing in this project" would be a claim about packages this
                   page did not look at. */
                <p className="text-muted">
                  No findings in the {totals.data.aggregated} package
                  {totals.data.aggregated === 1 ? '' : 's'} counted here.
                  {totals.data.packages === SAMPLE_LIMIT && ' There may be older packages beyond this page.'}
                </p>
              ) : (
                <div className="usage-breakdown">
                  {/* Every outcome, including the abstentions, and they sum to the total. Showing only
                      passes and failures invites a reader to treat the remainder as passing — and
                      under exact match the abstentions are the bulk of a run, not an edge case. */}
                  <OutcomeRow label={OUTCOME_LABELS.PASS} count={totals.data.counts.passed} total={totals.data.counts.total} cls="usage-bar--pass" />
                  <OutcomeRow label={OUTCOME_LABELS.FAIL} count={totals.data.counts.failed} total={totals.data.counts.total} cls="usage-bar--fail" />
                  <OutcomeRow label={OUTCOME_LABELS.REVIEW_REQUIRED} count={totals.data.counts.review_required} total={totals.data.counts.total} cls="usage-bar--review" />
                  <OutcomeRow label={OUTCOME_LABELS.NOT_FOUND} count={totals.data.counts.not_found} total={totals.data.counts.total} cls="usage-bar--missing" />
                  <OutcomeRow label={OUTCOME_LABELS.NO_APPLICABLE_RULE} count={totals.data.counts.no_applicable_rule} total={totals.data.counts.total} cls="usage-bar--missing" />
                </div>
              )}
            </div>

            <div className="usage-section">
              <h2 className="usage-section__title">Recent activity</h2>
              <p className="text-muted">
                Not available. Reviewer actions are recorded per review session and there is no
                endpoint that lists them across a project.
              </p>
            </div>
          </>
        )}
      </div>
    </PageFrame>
  );
}

function StatCard({ value, label, sub }: { value: string; label: string; sub: string }) {
  return (
    <div className="usage-stat-card">
      <span className="usage-stat-card__value">{value}</span>
      <span className="usage-stat-card__label">{label}</span>
      <span className="usage-stat-card__sub">{sub}</span>
    </div>
  );
}

function OutcomeRow({ label, count, total, cls }: { label: string; count: number; total: number; cls: string }) {
  const pct = total === 0 ? 0 : Math.round((count / total) * 100);
  return (
    <div className="usage-breakdown__row">
      <span className="usage-breakdown__label">{label}</span>
      <div className="usage-breakdown__bar-wrap">
        <div className={`usage-breakdown__bar ${cls}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="usage-breakdown__pct">{pct}%</span>
      <span className="usage-breakdown__count">{count}</span>
    </div>
  );
}

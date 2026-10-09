/**
 * The rules the engine would actually apply.
 *
 * This page used to render a hardcoded list, and it had drifted past "placeholder" into wrong: it
 * described every check as `within_tolerance` with a `± 3.175 mm` band, when V1 was decided as exact
 * match with no band at all, and it stated tolerances in millimetres, which are never a verdict
 * operand. A reviewer reading it would have come away believing the system works a way it does not.
 *
 * So it reads from `GET /api/v1/rules`, and shows only fields the API returns. Where the old list had
 * a formula, an applicability expression and a list of operands, this shows nothing — those are not
 * on the wire yet, and inventing them is how the previous version went wrong.
 *
 * Since #1072: a products × check types grid on top (a cell filters the table), the severity and
 * the release note every rule shares said once, and the rules as one sortable, searchable table.
 */

import { useState } from 'react';
import { listRules } from '../api/client';
import { useAsync } from '../api/useAsync';
import { PageFrame, PageLoadError } from '../components/ui/PageFrame';
import { RuleGridCard, RulesTable } from '@/components/rulebook/rulebook-overview';
import { Button } from '@/components/ui/button';
import { productWord } from '@/lib/documents-table';
import { checkTypeWord, matchesRuleFilter, sharedReleaseNote, type RuleFilter } from '@/lib/rulebook-overview';
import { rulebookEmptyState } from './rulebookState';
import '../components/ui/PageFrame.css';

const DESCRIPTION = 'Published checks and the exact rule snapshots used by the engine.';

export function RulebookPage() {
  const [attempt, setAttempt] = useState(0);
  const rules = useAsync(() => listRules(), [attempt]);
  const [filter, setFilter] = useState<RuleFilter | null>(null);

  if (rules.status === 'loading') {
    return (
      <PageFrame title="Rulebook" description={DESCRIPTION}>
        <p className="page-frame__state" role="status">Loading the rulebook…</p>
      </PageFrame>
    );
  }

  // Failure and emptiness are different answers and must not look alike. "No rules" on a screen that
  // could not reach the server would read as *this system checks nothing*, which is a claim nobody
  // made.
  if (rules.status === 'error') {
    return (
      <PageFrame title="Rulebook" description={DESCRIPTION}>
        <PageLoadError title="The rulebook could not be loaded" message={`${rules.error.message} The published rules are unavailable; this does not mean there are no rules.`} onRetry={() => setAttempt((value) => value + 1)} />
      </PageFrame>
    );
  }

  const all = rules.data;
  if (all.length === 0) {
    const empty = rulebookEmptyState(0);
    return (
      <PageFrame title="Rulebook" description={DESCRIPTION}>
        <div data-tw className="flex flex-col gap-1 font-sans">
          <h2 className="text-base font-semibold">{empty.title}</h2>
          <p className="text-sm text-muted-foreground">{empty.message}</p>
        </div>
      </PageFrame>
    );
  }

  const shown = all.filter((rule) => matchesRuleFilter(rule, filter));
  return (
    <PageFrame title="Rulebook" description={DESCRIPTION}>
      <div data-tw className="flex flex-col gap-4 font-sans">
        <RuleGridCard rules={all} filter={filter} onFilter={setFilter} />
        <section aria-labelledby="rules-title" className="flex flex-col gap-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 id="rules-title" className="text-base font-semibold">
              <span className="num">{all.length}</span> {all.length === 1 ? 'rule' : 'rules'} published
            </h2>
            {filter && (
              <p className="flex items-center gap-2 text-sm" role="status">
                Showing {productWord(filter.product).toLowerCase()}
                {filter.checkType && <> · {checkTypeWord(filter.checkType).toLowerCase()}</>}: <span className="num">{shown.length}</span>
                <Button type="button" size="sm" variant="outline" onClick={() => setFilter(null)}>Show all rules</Button>
              </p>
            )}
          </div>
          <RulesTable rules={shown} sharedNote={sharedReleaseNote(all)} emptyMessage={rulebookEmptyState(all.length).message} />
        </section>
      </div>
    </PageFrame>
  );
}

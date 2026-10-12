import { Hourglass } from 'lucide-react';
import { Cell, Label, Pie, PieChart } from 'recharts';

import type { Bucket } from '@/lib/countertop-results';
import { RESULT_LABEL } from '@/lib/countertop-results';
import type { Outcome } from '@/data/types';
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from '@/components/ui/chart';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';
import { cn } from '@/lib/utils';

const ORDER: Bucket[] = ['needs-you', 'not-checked', 'fail', 'pass', 'not-checkable'];
// "Not checked" (#1179) is not an outcome: an hourglass (drawn below) and its word, in the plain grey.
const GLYPH: Record<Bucket, Outcome> = { 'needs-you': 'REVIEW_REQUIRED', 'not-checked': 'NOT_FOUND', fail: 'FAIL', pass: 'PASS', 'not-checkable': 'NOT_FOUND' };
const FILL: Record<Bucket, string> = {
  'needs-you': 'var(--outcome-review)',
  'not-checked': 'var(--muted-foreground)',
  fail: 'var(--outcome-fail)',
  pass: 'var(--outcome-pass)',
  'not-checkable': 'var(--outcome-missing)',
};
const TONE: Record<Bucket, string> = {
  'needs-you': 'text-outcome-review-fg',
  'not-checked': 'text-muted-foreground',
  fail: 'text-outcome-fail-fg',
  pass: 'text-outcome-pass-fg',
  'not-checkable': 'text-outcome-missing-fg',
};

/**
 * The slices whose count no card shows. "Needs your decision" is not one of them (#1179): the
 * "Countertops need you" card is the one count of countertops waiting for the reviewer, so the
 * legend does not show a second, different number beside the same words.
 */
const VISIBLE_COUNT = new Set<Bucket>(['not-checked', 'not-checkable']);

const config = Object.fromEntries(ORDER.map((b) => [b, { label: RESULT_LABEL[b], color: FILL[b] }])) satisfies ChartConfig;

/**
 * Countertop outcomes as one donut (#1039), by recorded result (#1056): a FAIL is in the FAIL slice
 * even while it waits for the reviewer, and the legend says how many FAILs still need them. The
 * legend carries each group's glyph and word, so the chart reads the same in greyscale.
 *
 * Counts once (#1126): the cards beside it already show the PASS and FAIL numbers, and the
 * "Countertops need you" card the countertops waiting for the reviewer (#1179), so the legend shows a
 * number only for the slices no card counts: "Not checked" (no result yet: the checks have not run
 * on them) and "Not checkable". Every count stays in the legend for screen readers (the ring itself
 * is hidden from them) and in the tooltip. A slice with nothing in it is left out of the legend
 * when it is "Not checked", which most reviews never have.
 *
 * On a phone only the legend is shown: the ring would push the first countertop down. Loaded
 * lazily — the chart library is only fetched when a review has results to draw.
 */
export default function OutcomeChart({ counts, failNeedsYou = 0 }: { counts: Record<Bucket, number>; failNeedsYou?: number }) {
  const total = ORDER.reduce((sum, b) => sum + counts[b], 0);
  const data = ORDER.filter((b) => counts[b] > 0).map((b) => ({ bucket: b, count: counts[b], fill: FILL[b] }));
  return (
    <div data-slot="outcome-chart" className="flex items-center gap-4">
      <ChartContainer config={config} className="hidden aspect-square h-32 shrink-0 sm:flex sm:h-36" aria-hidden="true">
        <PieChart accessibilityLayer={false}>
          <ChartTooltip content={<ChartTooltipContent nameKey="bucket" hideLabel />} />
          <Pie data={data} dataKey="count" nameKey="bucket" innerRadius="62%" strokeWidth={2} stroke="var(--background)" isAnimationActive={false}>
            {data.map((d) => (
              <Cell key={d.bucket} fill={d.fill} />
            ))}
            <Label
              content={({ viewBox }) => {
                if (!viewBox || !('cx' in viewBox)) return null;
                return (
                  <text x={viewBox.cx} y={viewBox.cy} textAnchor="middle" dominantBaseline="middle" className="num fill-foreground text-xl">
                    {total}
                  </text>
                );
              }}
            />
          </Pie>
        </PieChart>
      </ChartContainer>
      <ul className="flex flex-wrap gap-x-4 gap-y-1.5 text-sm sm:flex-col sm:flex-nowrap" aria-label="Countertop outcomes">
        {ORDER.filter((b) => b !== 'not-checked' || counts[b] > 0).map((b) => (
          <li key={b} className="flex items-center gap-2 whitespace-nowrap" data-bucket={b}>
            {/* "Not checked" is not an outcome: its own glyph, never the dashed circle of "Not checkable". */}
            {b === 'not-checked' ? <Hourglass size={15} className={TONE[b]} aria-hidden="true" /> : <OutcomeIcon outcome={GLYPH[b]} size={15} className={TONE[b]} />}
            <span className="sm:min-w-24">{RESULT_LABEL[b]}</span>
            <span className={cn('num font-medium', !VISIBLE_COUNT.has(b) && 'sr-only')}>{counts[b]}</span>
            {/* Words only on screen (#1179): the number is the card's. Said in full to a screen reader. */}
            {b === 'fail' && failNeedsYou > 0 && (
              <span className="inline-flex items-center gap-1 text-xs text-outcome-review-fg" data-slot="fail-needs-you">
                <OutcomeIcon outcome="REVIEW_REQUIRED" size={12} />
                <span className="sr-only"><span className="num">{failNeedsYou}</span> {failNeedsYou === 1 ? 'FAIL' : 'FAILs'}{' '}</span>
                {failNeedsYou === 1 ? 'needs you' : 'need you'}
              </span>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

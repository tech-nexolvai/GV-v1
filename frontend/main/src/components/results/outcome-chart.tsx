import { Cell, Label, Pie, PieChart } from 'recharts';

import type { Bucket } from '@/lib/countertop-results';
import { RESULT_LABEL } from '@/lib/countertop-results';
import type { Outcome } from '@/data/types';
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from '@/components/ui/chart';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';
import { cn } from '@/lib/utils';

const ORDER: Bucket[] = ['needs-you', 'fail', 'pass', 'not-checkable'];
const GLYPH: Record<Bucket, Outcome> = { 'needs-you': 'REVIEW_REQUIRED', fail: 'FAIL', pass: 'PASS', 'not-checkable': 'NOT_FOUND' };
const FILL: Record<Bucket, string> = {
  'needs-you': 'var(--outcome-review)',
  fail: 'var(--outcome-fail)',
  pass: 'var(--outcome-pass)',
  'not-checkable': 'var(--outcome-missing)',
};
const TONE: Record<Bucket, string> = {
  'needs-you': 'text-outcome-review-fg',
  fail: 'text-outcome-fail-fg',
  pass: 'text-outcome-pass-fg',
  'not-checkable': 'text-outcome-missing-fg',
};

/** The slices whose count no card shows. */
const VISIBLE_COUNT = new Set<Bucket>(['needs-you', 'not-checkable']);

const config = Object.fromEntries(ORDER.map((b) => [b, { label: RESULT_LABEL[b], color: FILL[b] }])) satisfies ChartConfig;

/**
 * Countertop outcomes as one donut (#1039), by recorded result (#1056): a FAIL is in the FAIL slice
 * even while it waits for the reviewer, and the legend says how many FAILs still need them. The
 * legend carries each group's glyph and word, so the chart reads the same in greyscale.
 *
 * Counts once (#1126): the cards beside it already show the PASS and FAIL numbers, so the legend
 * shows a number only for the slices no card counts: "Needs your decision" (rows waiting with no PASS
 * or FAIL; the "Needs you" card also counts FAILs that wait) and "Not checkable". Every count stays
 * in the legend for screen readers (the ring itself is hidden from them) and in the tooltip.
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
        {ORDER.map((b) => (
          <li key={b} className="flex items-center gap-2 whitespace-nowrap">
            <OutcomeIcon outcome={GLYPH[b]} size={15} className={TONE[b]} />
            <span className="sm:min-w-24">{RESULT_LABEL[b]}</span>
            <span className={cn('num font-medium', !VISIBLE_COUNT.has(b) && 'sr-only')}>{counts[b]}</span>
            {b === 'fail' && failNeedsYou > 0 && (
              <span className="inline-flex items-center gap-1 text-xs text-outcome-review-fg" data-slot="fail-needs-you">
                <OutcomeIcon outcome="REVIEW_REQUIRED" size={12} />
                <span className="num">{failNeedsYou}</span> {failNeedsYou === 1 ? 'needs you' : 'need you'}
              </span>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

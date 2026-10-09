import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from 'recharts';

import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from '@/components/ui/chart';
import { OUTCOME_FILL } from '@/components/ui/outcome-badge';
import { OUTCOME_LABELS } from '@/data/outcomeLabels';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';
import { formatUsd, outcomeTotals, type CostDay, type OutcomeDay } from '@/lib/usage';

/**
 * The Usage charts (#1072). Loaded lazily, so the chart library is fetched only when Usage opens.
 * Each chart is hidden from screen readers (and from the keyboard: recharts' own focusable layer is
 * off) and followed by a visually hidden table with the same numbers. Recorded results also get a
 * visible list with each outcome's shape, word and count, so they never rest on colour alone.
 */

/** Axis money: whole cents, or tenths of a cent for days that cost less than a cent. */
function axisUsd(value: number): string {
  if (value === 0) return '$0';
  return value < 0.01 ? `$${value.toFixed(3)}` : `$${value.toFixed(2)}`;
}

const costConfig = { cost: { label: 'Cost (USD)', color: 'var(--chart-2)' } } satisfies ChartConfig;
/** A few days (or sets) should not draw bars half the chart wide. */
const MAX_BAR = 56;

export function CostByDayChart({ days }: { days: readonly CostDay[] }) {
  return (
    <div data-slot="cost-chart">
      <ChartContainer config={costConfig} className="aspect-auto h-48 w-full" aria-hidden="true">
        <BarChart data={[...days]} margin={{ left: 4, right: 4 }} accessibilityLayer={false}>
          <CartesianGrid vertical={false} />
          <XAxis dataKey="label" tickLine={false} axisLine={false} tickMargin={8} />
          <YAxis tickLine={false} axisLine={false} width={56} tickFormatter={axisUsd} />
          <ChartTooltip
            content={(
              <ChartTooltipContent
                labelFormatter={(_, payload) => (payload?.[0]?.payload as CostDay | undefined)?.fullLabel ?? ''}
                formatter={(value, _name, item) => {
                  const day = item.payload as CostDay;
                  return day.unpriced > 0 ? `at least ${formatUsd(String(value))} (${day.unpriced} not priced)` : formatUsd(String(value));
                }}
              />
            )}
          />
          <Bar dataKey="cost" fill="var(--chart-2)" radius={[4, 4, 0, 0]} maxBarSize={MAX_BAR} isAnimationActive={false} />
        </BarChart>
      </ChartContainer>
      <table className="sr-only">
        <caption>AI cost by day (UTC)</caption>
        <thead><tr><th scope="col">Day</th><th scope="col">Calls</th><th scope="col">Failed</th><th scope="col">Not priced</th><th scope="col">Cost</th></tr></thead>
        <tbody>
          {days.map((day) => (
            <tr key={day.day}>
              <th scope="row">{day.fullLabel}</th><td>{day.calls}</td><td>{day.failed}</td><td>{day.unpriced}</td>
              <td>{day.unpriced > 0 ? `at least ${formatUsd(String(day.cost))}` : formatUsd(String(day.cost))}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const STACK = [
  { key: 'pass', outcome: 'PASS' },
  { key: 'fail', outcome: 'FAIL' },
  { key: 'review', outcome: 'REVIEW_REQUIRED' },
  { key: 'not_found', outcome: 'NOT_FOUND' },
  { key: 'no_rule', outcome: 'NO_APPLICABLE_RULE' },
] as const;

const outcomeConfig = Object.fromEntries(
  STACK.map(({ key, outcome }) => [key, { label: OUTCOME_LABELS[outcome], color: OUTCOME_FILL[outcome] }]),
) satisfies ChartConfig;

export function OutcomesByDayChart({ days }: { days: readonly OutcomeDay[] }) {
  const totals = outcomeTotals(days);
  return (
    <div data-slot="outcomes-chart" className="flex flex-col gap-3">
      <ul className="flex flex-wrap gap-x-4 gap-y-1 text-sm" aria-label="Recorded results in all">
        {STACK.map(({ key, outcome }) => (
          <li key={key} className="inline-flex items-center gap-1.5">
            <span className="inline-block size-2.5 rounded-sm" style={{ background: OUTCOME_FILL[outcome] }} aria-hidden="true" />
            <OutcomeIcon outcome={outcome} size={13} />
            {OUTCOME_LABELS[outcome]} <span className="num font-medium">{totals[key]}</span>
          </li>
        ))}
      </ul>
      <ChartContainer config={outcomeConfig} className="aspect-auto h-48 w-full" aria-hidden="true">
        <BarChart data={[...days]} margin={{ left: 4, right: 4 }} accessibilityLayer={false}>
          <CartesianGrid vertical={false} />
          <XAxis dataKey="label" tickLine={false} axisLine={false} tickMargin={8} />
          <YAxis allowDecimals={false} tickLine={false} axisLine={false} width={36} />
          <ChartTooltip content={<ChartTooltipContent labelFormatter={(_, payload) => (payload?.[0]?.payload as OutcomeDay | undefined)?.fullLabel ?? ''} />} />
          {STACK.map(({ key, outcome }, index) => (
            <Bar
              key={key}
              dataKey={key}
              stackId="results"
              // The defined outcome tokens themselves (the chart's runtime --color-* names are not
              // visible to the design-token check, tests/test_frontend_tokens.py).
              fill={OUTCOME_FILL[outcome]}
              radius={index === STACK.length - 1 ? [4, 4, 0, 0] : undefined}
              maxBarSize={MAX_BAR}
              isAnimationActive={false}
            />
          ))}
        </BarChart>
      </ChartContainer>
      <table className="sr-only">
        <caption>Recorded results by the day each drawing set was uploaded (UTC)</caption>
        <thead>
          <tr>
            <th scope="col">Day</th><th scope="col">Sets</th>
            {STACK.map(({ key, outcome }) => <th key={key} scope="col">{OUTCOME_LABELS[outcome]}</th>)}
          </tr>
        </thead>
        <tbody>
          {days.map((day) => (
            <tr key={day.day}>
              <th scope="row">{day.fullLabel}</th><td>{day.sets}</td>
              {STACK.map(({ key }) => <td key={key}>{day[key]}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

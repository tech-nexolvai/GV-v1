import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from 'recharts';

import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from '@/components/ui/chart';
import { OUTCOME_FILL } from '@/components/ui/outcome-badge';
import { OUTCOME_LABELS } from '@/data/outcomeLabels';
import { formatUsd, type CostDay, type OutcomeDay } from '@/lib/usage';

/**
 * The Usage charts (#1072). Loaded lazily, so the chart library is fetched only when Usage opens.
 * Each chart is hidden from screen readers and followed by a visually hidden table with the same
 * numbers; sighted readers get the axis, the tooltip and, for results, the legend's words.
 */

const costConfig = { cost: { label: 'Cost (USD)', color: 'var(--chart-2)' } } satisfies ChartConfig;
/** A few days (or sets) should not draw bars half the chart wide. */
const MAX_BAR = 56;

export function CostByDayChart({ days }: { days: readonly CostDay[] }) {
  return (
    <div data-slot="cost-chart">
      <ChartContainer config={costConfig} className="aspect-auto h-48 w-full" aria-hidden="true">
        <BarChart data={[...days]} margin={{ left: 4, right: 4 }}>
          <CartesianGrid vertical={false} />
          <XAxis dataKey="label" tickLine={false} axisLine={false} tickMargin={8} />
          <YAxis tickLine={false} axisLine={false} width={56} tickFormatter={(value: number) => `$${value.toFixed(2)}`} />
          <ChartTooltip content={<ChartTooltipContent formatter={(value) => formatUsd(String(value))} />} />
          <Bar dataKey="cost" fill="var(--color-cost)" radius={[4, 4, 0, 0]} maxBarSize={MAX_BAR} isAnimationActive={false} />
        </BarChart>
      </ChartContainer>
      <table className="sr-only">
        <caption>AI cost by day (UTC)</caption>
        <thead><tr><th scope="col">Day</th><th scope="col">Calls</th><th scope="col">Failed</th><th scope="col">Cost</th></tr></thead>
        <tbody>
          {days.map((day) => (
            <tr key={day.day}><th scope="row">{day.label}</th><td>{day.calls}</td><td>{day.failed}</td><td>{formatUsd(String(day.cost))}</td></tr>
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
  return (
    <div data-slot="outcomes-chart">
      <ChartContainer config={outcomeConfig} className="aspect-auto h-48 w-full" aria-hidden="true">
        <BarChart data={[...days]} margin={{ left: 4, right: 4 }}>
          <CartesianGrid vertical={false} />
          <XAxis dataKey="label" tickLine={false} axisLine={false} tickMargin={8} />
          <YAxis allowDecimals={false} tickLine={false} axisLine={false} width={36} />
          <ChartTooltip content={<ChartTooltipContent />} />
          {STACK.map(({ key }, index) => (
            <Bar
              key={key}
              dataKey={key}
              stackId="results"
              fill={`var(--color-${key})`}
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
              <th scope="row">{day.label}</th><td>{day.sets}</td>
              {STACK.map(({ key }) => <td key={key}>{day[key]}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

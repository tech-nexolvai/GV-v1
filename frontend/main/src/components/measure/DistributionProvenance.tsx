import { CABINET_TYPE_LABELS, type FillerDistributionRequest } from './fillerDistribution.js';
import { cn } from '@/lib/utils';

const LIMIT_LABELS = {
  filler_min: 'Minimum filler width', filler_max: 'Maximum filler width',
  single_door_cab_width_min: 'Single-door cabinet minimum', single_door_cab_width_max: 'Single-door cabinet maximum',
  double_door_cab_width_min: 'Double-door cabinet minimum', double_door_cab_width_max: 'Double-door cabinet maximum',
  drawer_cab_width_min: 'Drawer cabinet minimum', drawer_cab_width_max: 'Drawer cabinet maximum',
} as const;

/** A definition row: the limit's name, and its value in the mono face. */
const ROW = 'flex flex-wrap justify-between gap-x-3 border-b py-1 last:border-0';

export function DistributionProvenance({ request, current, busy }: {
  request: FillerDistributionRequest | null; current: boolean; busy: boolean;
}) {
  return <>
    {!current && <div className="flex flex-col gap-1 rounded-lg border border-outcome-review-fg/40 bg-outcome-review-bg p-3 text-sm" role="status">
      <strong className="font-medium text-outcome-review-fg">Earlier result — inputs have changed</strong>
      <p className="text-xs text-foreground">{request ? 'The preview below belongs to the inputs recorded with it.'
        : 'The inputs used for this preview were not recorded here.'}{' '}
        {busy ? 'A new calculation is in progress.' : 'Calculate again to preview your current entries.'}
      </p>
    </div>}
    {current && busy && <p className="text-xs text-muted-foreground" role="status">Recalculating…</p>}
    {request && <details className="text-sm">
      <summary className="cursor-pointer py-2 text-xs text-muted-foreground">Inputs used for this preview</summary>
      <dl className="text-xs">
        <div className={ROW}><dt className="text-muted-foreground">Site field width</dt><dd className="num">{request.field_width ?? 'Not supplied'}</dd></div>
        {request.assembly.cabinets.map((cabinet, index) => <div key={cabinet.id} className={ROW}>
          <dt className="text-muted-foreground">Cabinet {index + 1} · {CABINET_TYPE_LABELS.find(item => item.value === cabinet.type)?.label ?? cabinet.type}</dt>
          <dd className="num">{cabinet.width}</dd>
        </div>)}
        {request.assembly.fillers.map((filler, index) => <div key={filler.id} className={ROW}>
          <dt className="text-muted-foreground">Filler {index + 1}</dt><dd className="num">{filler.width}</dd>
        </div>)}
        {(Object.keys(LIMIT_LABELS) as (keyof typeof LIMIT_LABELS)[]).map(key => <div key={key} className={cn(ROW)}>
          <dt className="text-muted-foreground">{LIMIT_LABELS[key]}</dt><dd className="num">{request[key]}</dd>
        </div>)}
      </dl>
    </details>}
  </>;
}

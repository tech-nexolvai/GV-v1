import { CABINET_TYPE_LABELS, type FillerDistributionRequest } from './fillerDistribution.js';

const LIMIT_LABELS = {
  filler_min: 'Minimum filler width', filler_max: 'Maximum filler width',
  single_door_cab_width_min: 'Single-door cabinet minimum', single_door_cab_width_max: 'Single-door cabinet maximum',
  double_door_cab_width_min: 'Double-door cabinet minimum', double_door_cab_width_max: 'Double-door cabinet maximum',
  drawer_cab_width_min: 'Drawer cabinet minimum', drawer_cab_width_max: 'Drawer cabinet maximum',
} as const;

export function DistributionProvenance({ request, current, busy }: {
  request: FillerDistributionRequest | null; current: boolean; busy: boolean;
}) {
  return <>
    {!current && <div className="distribution-result__notice" role="status">
      <strong>Earlier result — inputs have changed</strong>
      <p>{request ? 'The preview below belongs to the inputs recorded with it.'
        : 'The inputs used for this preview were not recorded here.'}{' '}
        {busy ? 'A new calculation is in progress.' : 'Calculate again to preview your current entries.'}
      </p>
    </div>}
    {current && busy && <p className="enter-values__hint" role="status">Recalculating…</p>}
    {request && <details className="distribution-result__inputs">
      <summary>Inputs used for this preview</summary>
      <dl>
        <div><dt>Site field width</dt><dd>{request.field_width ?? 'Not supplied'}</dd></div>
        {request.assembly.cabinets.map((cabinet, index) => <div key={cabinet.id}>
          <dt>Cabinet {index + 1} · {CABINET_TYPE_LABELS.find(item => item.value === cabinet.type)?.label ?? cabinet.type}</dt>
          <dd>{cabinet.width}</dd>
        </div>)}
        {request.assembly.fillers.map((filler, index) => <div key={filler.id}>
          <dt>Filler {index + 1}</dt><dd>{filler.width}</dd>
        </div>)}
        {(Object.keys(LIMIT_LABELS) as (keyof typeof LIMIT_LABELS)[]).map(key => <div key={key}>
          <dt>{LIMIT_LABELS[key]}</dt><dd>{request[key]}</dd>
        </div>)}
      </dl>
    </details>}
  </>;
}

/**
 * What a run for this package would use for a setting now (#1181): GV's default, the sink card's
 * size, a value for this job or this package, or the rulebook's own default — instead of a blank box
 * that reads as "nothing". Nothing in force says so: the check waits, and nothing is zero.
 */
import type { components } from '@/api/schema';

type InForce = components['schemas']['InForceOut'];

const FROM: Record<InForce['origin'], string> = {
  rulebook: "the rulebook's default",
  gv_default: "GV's default",
  sink_card: 'from the sink card',
  project: 'set for this job',
  package: 'typed for this package',
};

export function InForceNote({ inForce }: { inForce: InForce | null | undefined }) {
  if (!inForce) {
    return (
      <span className="text-xs text-muted-foreground" data-slot="in-force" data-origin="none">
        In use: nothing yet — the checks that need it wait.
      </span>
    );
  }
  return (
    <span className="text-xs text-muted-foreground" data-slot="in-force" data-origin={inForce.origin}>
      In use: <span className="num text-foreground">{inForce.text}</span> ({FROM[inForce.origin]}
      {inForce.set_by ? `, ${inForce.set_by}` : ''})
    </span>
  );
}

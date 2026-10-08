import type { components } from '../../api/schema';
import { parseChangedValue } from '@/lib/changed-values';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';

type ChangedValues = components['schemas']['ChangedValuesOut'];

interface Props {
  value: ChangedValues | null;
  state: 'loading' | 'error' | 'ready';
  currentRevisionId: string | null;
}

/**
 * A presentation of the pinned check-run report, never the newest settings.
 *
 * Two parts, each under its own heading: the project values that set a GV standard aside (as a
 * table), and the required values nobody has set. Each says plainly when it is empty — once.
 */
export function ChangedValuesPanel({ value, state, currentRevisionId }: Props) {
  let content: React.ReactNode;
  if (state === 'loading') {
    content = <p className="text-sm text-muted-foreground">Loading recorded check-run values…</p>;
  } else if (state === 'error' || value === null) {
    content = <p role="alert" className="text-sm">The recorded check-run values could not be loaded.</p>;
  } else if (currentRevisionId === null || value.revision_id !== currentRevisionId) {
    content = <p className="text-sm">Check-run values belong to another revision. Refresh this review.</p>;
  } else if (value.message !== null) {
    content = <p className="text-sm">{value.message}</p>;
  } else {
    const rows = value.company_standards_displaced.map(parseChangedValue);
    content = (
      <div className="flex flex-col gap-4">
        <section aria-labelledby="changed-values-differ" className="flex flex-col gap-2">
          <h3 id="changed-values-differ" className="text-sm font-medium">Different from the GV standard</h3>
          {rows.length === 0 ? (
            <p className="text-sm text-muted-foreground">No project value differs from the GV standard.</p>
          ) : (
            <div className="overflow-x-auto rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Setting</TableHead>
                    <TableHead>GV standard</TableHead>
                    <TableHead>This project</TableHead>
                    <TableHead>Source</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((row, index) =>
                    row.kind === 'parsed' ? (
                      <TableRow key={index} title={row.raw}>
                        <TableCell className="num text-xs">{row.setting}</TableCell>
                        <TableCell className="num">{row.standard ?? '—'}</TableCell>
                        <TableCell className="num font-medium">{row.project}</TableCell>
                        <TableCell className="max-w-48 truncate text-muted-foreground" title={row.source}>{row.source}</TableCell>
                      </TableRow>
                    ) : (
                      <TableRow key={index}>
                        <TableCell colSpan={4} className="whitespace-normal">{row.raw}</TableCell>
                      </TableRow>
                    ),
                  )}
                </TableBody>
              </Table>
            </div>
          )}
        </section>
        <section aria-labelledby="changed-values-outstanding" className="flex flex-col gap-2">
          <h3 id="changed-values-outstanding" className="text-sm font-medium">Required values not set</h3>
          {value.outstanding.length ? (
            <ul className="flex flex-wrap gap-1.5">
              {value.outstanding.map((item) => (
                <li key={item} className="num rounded-md border border-dashed px-1.5 py-0.5 text-xs text-muted-foreground">{item}</li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-muted-foreground">Every required value is set.</p>
          )}
        </section>
      </div>
    );
  }
  return (
    <section data-slot="changed-values" aria-labelledby="changed-values-title" className="flex flex-col gap-3 font-sans">
      <h2 id="changed-values-title" className="text-base font-semibold">Project values that differ from GV standards</h2>
      {content}
    </section>
  );
}

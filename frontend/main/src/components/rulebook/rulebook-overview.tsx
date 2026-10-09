import type { ColumnDef } from '@tanstack/react-table';
import { AlertTriangle, CheckCircle2 } from 'lucide-react';

import type { Rule } from '@/api/client';
import { DataTable, SortableHeader } from '@/components/data-table/data-table';
import { InfoTip } from '@/components/ui/info-tip';
import { cn } from '@/lib/utils';
import { productWord } from '@/lib/documents-table';
import {
  checkTypeWord,
  ruleGrid,
  severityCounts,
  sharedReleaseNote,
  type RuleFilter,
} from '@/lib/rulebook-overview';

function rulesWord(count: number) {
  return count === 1 ? 'rule' : 'rules';
}

/**
 * Products × check types (#1072): how many published rules check what, and where their two sides
 * come from. A cell with rules filters the table below; selecting it again shows every rule.
 */
export function RuleGridCard({
  rules,
  filter,
  onFilter,
}: {
  rules: readonly Rule[];
  filter: RuleFilter | null;
  onFilter: (filter: RuleFilter | null) => void;
}) {
  const grid = ruleGrid(rules);
  const severities = severityCounts(rules);
  const releasable = rules.filter((rule) => rule.production_ready).length;
  const sharedNote = sharedReleaseNote(rules);
  const pick = (next: RuleFilter) =>
    onFilter(filter && filter.product === next.product && filter.checkType === next.checkType ? null : next);

  return (
    <section data-slot="rule-grid" aria-labelledby="rule-grid-title" className="flex flex-col gap-3 rounded-xl border p-4">
      <h2 id="rule-grid-title" className="flex items-center gap-1.5 text-base font-semibold">
        What the rulebook checks
        <InfoTip label="About the rulebook">
          <p>Rules are authored in YAML, validated with Pydantic and JSON Schema, and stored as immutable snapshots. Publishing needs human approval and a full gold-set regression.</p>
          <p><strong>Shop drawing only:</strong> both sides come from the vendor&apos;s drawing. <strong>Architect vs shop:</strong> the expected value comes from the architect&apos;s set. <strong>Against a standard:</strong> the drawing is compared with a fixed number.</p>
        </InfoTip>
      </h2>
      <div className="overflow-x-auto">
        <table className="w-full text-sm" aria-labelledby="rule-grid-title">
          <thead>
            <tr className="border-b text-left">
              <th scope="col" className="py-1.5 pr-3 font-medium">Product</th>
              {grid.checkTypes.map((type) => (
                <th key={type} scope="col" className="px-3 py-1.5 text-right font-medium">{checkTypeWord(type)}</th>
              ))}
              <th scope="col" className="pl-3 py-1.5 text-right font-medium">All</th>
            </tr>
          </thead>
          <tbody>
            {grid.products.map((product) => (
              <tr key={product} className="border-b last:border-b-0">
                <th scope="row" className="py-1.5 pr-3 text-left font-normal">{productWord(product)}</th>
                {grid.checkTypes.map((type) => {
                  const count = grid.count(product, type);
                  const active = filter?.product === product && filter.checkType === type;
                  return (
                    <td key={type} className="px-3 py-1.5 text-right">
                      {count === 0 ? (
                        <span className="text-muted-foreground"><span aria-hidden="true">—</span><span className="sr-only">none</span></span>
                      ) : (
                        <button
                          type="button"
                          aria-pressed={active}
                          aria-label={`${count} ${productWord(product).toLowerCase()} ${rulesWord(count)}, ${checkTypeWord(type).toLowerCase()}`}
                          onClick={() => pick({ product, checkType: type })}
                          className={cn('num rounded-md px-2 py-0.5 hover:bg-accent focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none', active && 'bg-foreground text-background hover:bg-foreground')}
                        >
                          {count}
                        </button>
                      )}
                    </td>
                  );
                })}
                <td className="pl-3 py-1.5 text-right">
                  <button
                    type="button"
                    aria-pressed={filter?.product === product && filter.checkType === null}
                    aria-label={`All ${grid.byProduct(product)} ${productWord(product).toLowerCase()} ${rulesWord(grid.byProduct(product))}`}
                    onClick={() => pick({ product, checkType: null })}
                    className={cn('num rounded-md px-2 py-0.5 font-medium hover:bg-accent focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none', filter?.product === product && filter.checkType === null && 'bg-foreground text-background hover:bg-foreground')}
                  >
                    {grid.byProduct(product)}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr className="border-t text-muted-foreground">
              <th scope="row" className="py-1.5 pr-3 text-left font-normal">All products</th>
              {grid.checkTypes.map((type) => (
                <td key={type} className="num px-3 py-1.5 text-right">{grid.byCheckType(type)}</td>
              ))}
              <td className="num pl-3 py-1.5 text-right font-medium text-foreground">{grid.total}</td>
            </tr>
          </tfoot>
        </table>
      </div>
      <ul className="flex flex-col gap-1 text-sm text-muted-foreground">
        <li data-part="severity">
          {severities.length === 1 ? (
            <>
              {severities[0][1] === grid.total && grid.total > 1 ? 'Every rule has' : `${severities[0][1]} ${rulesWord(severities[0][1])} with`} severity{' '}
              <code className="text-foreground">{severities[0][0]}</code>. There is no severity split yet.
            </>
          ) : (
            <>
              Severity:{' '}
              {severities.map(([severity, count], index) => (
                <span key={severity}>{index > 0 && ' · '}<span className="num">{count}</span> <code className="text-foreground">{severity}</code></span>
              ))}
            </>
          )}
        </li>
        <li data-part="releasable">
          <span className="num">{releasable}</span> of <span className="num">{grid.total}</span> releasable
          {sharedNote ? <>: {sharedNote.replace(/^Releasable:\s*/i, '')}</> : '.'}
        </li>
      </ul>
    </section>
  );
}

const SHORT_HASH = 12;

function columns(sharedNote: string | null): ColumnDef<Rule>[] {
  return [
    {
      id: 'rule',
      accessorFn: (rule) => `${rule.rule_id} ${rule.name}`,
      header: ({ column }) => <SortableHeader column={column}>Rule</SortableHeader>,
      sortingFn: (a, b) => a.original.rule_id.localeCompare(b.original.rule_id),
      cell: ({ row }) => (
        <div className="flex min-w-48 flex-col">
          <code className="text-xs text-muted-foreground">{row.original.rule_id}</code>
          <span className="font-medium">{row.original.name}</span>
          {/* Only a note that differs from the one every rule shares (shown once above). */}
          {row.original.release_note.trim() !== '' && row.original.release_note.trim() !== sharedNote && (
            <span className="mt-0.5 text-xs text-muted-foreground" data-part="release-note">{row.original.release_note}</span>
          )}
        </div>
      ),
    },
    {
      id: 'product',
      accessorFn: (rule) => productWord(rule.product_type),
      header: ({ column }) => <SortableHeader column={column}>Product</SortableHeader>,
    },
    {
      id: 'check',
      accessorFn: (rule) => checkTypeWord(rule.check_type),
      header: ({ column }) => <SortableHeader column={column}>Compares</SortableHeader>,
      meta: { className: 'hidden md:table-cell' },
    },
    {
      id: 'severity',
      accessorFn: (rule) => rule.severity,
      header: 'Severity',
      enableSorting: false,
      meta: { className: 'hidden md:table-cell' },
      cell: ({ row }) => <code className="text-xs">{row.original.severity}</code>,
    },
    {
      id: 'version',
      accessorFn: (rule) => rule.version,
      header: 'Version',
      enableSorting: false,
      meta: { className: 'hidden lg:table-cell' },
      cell: ({ row }) => (
        <span className="text-xs whitespace-nowrap">
          <span className="num">v{row.original.version}</span>
          <span className="text-muted-foreground"> · <span className="num">{row.original.published_versions}</span> published</span>
        </span>
      ),
    },
    {
      id: 'release',
      accessorFn: (rule) => (rule.production_ready ? 0 : rule.unconfirmed_tolerances || 1),
      header: ({ column }) => <SortableHeader column={column}>Release</SortableHeader>,
      sortDescFirst: true,
      cell: ({ row }) =>
        row.original.production_ready ? (
          <span className="inline-flex items-center gap-1 text-xs whitespace-nowrap">
            <CheckCircle2 className="size-3.5" aria-hidden="true" /> Releasable
          </span>
        ) : (
          <span className="inline-flex items-center gap-1 text-xs font-medium whitespace-nowrap">
            <AlertTriangle className="size-3.5" aria-hidden="true" />
            {row.original.unconfirmed_tolerances > 0
              ? <><span className="num">{row.original.unconfirmed_tolerances}</span>&nbsp;unconfirmed {row.original.unconfirmed_tolerances === 1 ? 'tolerance' : 'tolerances'}</>
              : 'Not releasable'}
          </span>
        ),
    },
    {
      id: 'snapshot',
      accessorFn: (rule) => rule.snapshot_id,
      header: 'Snapshot',
      enableSorting: false,
      meta: { className: 'hidden xl:table-cell' },
      cell: ({ row }) => (
        <code className="text-xs text-muted-foreground" title={row.original.snapshot_id}>
          {row.original.snapshot_id.replace(/^sha256:/, '').slice(0, SHORT_HASH)}…
        </code>
      ),
    },
  ];
}

/**
 * The published rules as one sortable, searchable table (#1072). `sharedNote` is the release note
 * every published rule shares (shown once above), so a row shows only a note that differs from it.
 */
export function RulesTable({ rules, sharedNote, emptyMessage }: { rules: readonly Rule[]; sharedNote: string | null; emptyMessage: string }) {
  return (
    <DataTable
      label="Published rules"
      columns={columns(sharedNote)}
      data={[...rules]}
      getRowId={(rule) => rule.rule_id}
      initialSorting={[{ id: 'rule', desc: false }]}
      filterPlaceholder="Search rules"
      emptyMessage={emptyMessage}
    />
  );
}

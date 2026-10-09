import { useState } from 'react';
import {
  type RowData,
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getSortedRowModel,
  useReactTable,
  type Column,
  type ColumnDef,
  type SortingState,
} from '@tanstack/react-table';
import { ArrowDown, ArrowUp, ArrowUpDown } from 'lucide-react';

import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';

declare module '@tanstack/react-table' {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  interface ColumnMeta<TData extends RowData, TValue> {
    /** Classes for this column's header and cells, e.g. hiding a column on a phone. */
    className?: string;
  }
}

/**
 * A sortable, filterable table on shadcn's <Table> and TanStack Table v8.
 *
 * Kept deliberately small: sorting, one text filter, and an empty state that says what is empty.
 * Paging is left to the caller, because the API pages by cursor and a table that pages its own
 * slice would quietly hide rows it never loaded.
 */
export function DataTable<TData, TValue>({
  columns,
  data,
  filterPlaceholder,
  emptyMessage = 'Nothing to show.',
  getRowId,
  onRowClick,
  className,
  initialSorting = [],
  label,
}: {
  columns: ColumnDef<TData, TValue>[];
  data: TData[];
  /** Shows a filter box over every column's text when given. */
  filterPlaceholder?: string;
  emptyMessage?: string;
  getRowId?: (row: TData) => string;
  onRowClick?: (row: TData) => void;
  className?: string;
  /** The order the table opens in (the reviewer can change it). */
  initialSorting?: SortingState;
  /** The table's accessible name. */
  label?: string;
}) {
  const [sorting, setSorting] = useState<SortingState>(initialSorting);
  const [globalFilter, setGlobalFilter] = useState('');

  // TanStack Table returns new functions every render by design; the React Compiler is told so.
  // eslint-disable-next-line react-hooks/incompatible-library
  const table = useReactTable({
    data,
    columns,
    getRowId,
    state: { sorting, globalFilter },
    onSortingChange: setSorting,
    onGlobalFilterChange: setGlobalFilter,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
  });

  const rows = table.getRowModel().rows;

  return (
    <div data-slot="data-table" className={cn('flex flex-col gap-3', className)}>
      {filterPlaceholder && (
        <Input
          value={globalFilter}
          onChange={(event) => setGlobalFilter(event.target.value)}
          placeholder={filterPlaceholder}
          aria-label={filterPlaceholder}
          className="max-w-xs"
        />
      )}
      <div className="overflow-hidden rounded-lg border">
        <Table aria-label={label}>
          <TableHeader className="bg-muted/50">
            {table.getHeaderGroups().map((group) => (
              <TableRow key={group.id}>
                {group.headers.map((header) => (
                  <TableHead
                    key={header.id}
                    aria-sort={ariaSort(header.column.getIsSorted())}
                    className={header.column.columnDef.meta?.className}
                  >
                    {header.isPlaceholder
                      ? null
                      : flexRender(header.column.columnDef.header, header.getContext())}
                  </TableHead>
                ))}
              </TableRow>
            ))}
          </TableHeader>
          <TableBody>
            {rows.length === 0 ? (
              <TableRow>
                <TableCell colSpan={columns.length} className="h-20 text-center text-muted-foreground">
                  {globalFilter ? `Nothing matches “${globalFilter}”.` : emptyMessage}
                </TableCell>
              </TableRow>
            ) : (
              rows.map((row) => (
                <TableRow
                  key={row.id}
                  onClick={onRowClick ? () => onRowClick(row.original) : undefined}
                  className={onRowClick ? 'cursor-pointer' : undefined}
                >
                  {row.getVisibleCells().map((cell) => (
                    <TableCell key={cell.id} className={cell.column.columnDef.meta?.className}>
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </TableCell>
                  ))}
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}

/** A column header that sorts on click and shows which way it is sorted. */
export function SortableHeader<TData, TValue>({
  column,
  children,
  className,
  compact = false,
}: {
  column: Column<TData, TValue>;
  children: React.ReactNode;
  className?: string;
  /** Show the arrow only on the sorted column (dense tables); the header still sorts on click. */
  compact?: boolean;
}) {
  // TanStack hands back the same `column` object after every sort, so a memoised render would keep the
  // old direction: the second click sorted the same way again and the arrow never moved (#1064).
  'use no memo';
  const sorted = column.getIsSorted();
  const Icon = sorted === 'asc' ? ArrowUp : sorted === 'desc' ? ArrowDown : ArrowUpDown;
  const showIcon = !compact || Boolean(sorted);
  return (
    <Button
      variant="ghost"
      size="sm"
      className={cn('-ml-2 h-8 px-2 font-medium', className)}
      // The first click sorts low to high, unless the column asks for high first (`sortDescFirst`,
      // e.g. counts and dates). Other tables keep their low-to-high first click.
      onClick={() => column.toggleSorting(sorted ? sorted === 'asc' : column.columnDef.sortDescFirst === true)}
    >
      {children}
      {showIcon && <Icon className={cn('size-3.5', !sorted && 'text-muted-foreground')} aria-hidden="true" />}
    </Button>
  );
}

function ariaSort(sorted: false | 'asc' | 'desc'): 'ascending' | 'descending' | undefined {
  if (sorted === 'asc') return 'ascending';
  if (sorted === 'desc') return 'descending';
  return undefined;
}

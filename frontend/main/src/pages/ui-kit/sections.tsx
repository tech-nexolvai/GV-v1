import { useState } from 'react';
import type { ColumnDef } from '@tanstack/react-table';
import { Bar, BarChart, CartesianGrid, Cell, Label as ChartLabel, Pie, PieChart, XAxis, YAxis } from 'recharts';
import { toast } from 'sonner';
import {
  BookOpen,
  ChevronDown,
  FileText,
  FilePlus2,
  Info,
  MoreHorizontal,
  Ruler,
  Settings2,
} from 'lucide-react';

import { cn } from '@/lib/utils';
import type { Outcome } from '@/data/types';
import { OUTCOME_LABELS } from '@/data/outcomeLabels';
import { OutcomeBadge, OUTCOME_FILL } from '@/components/ui/outcome-badge';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';
import { DataTable, SortableHeader } from '@/components/data-table/data-table';
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from '@/components/ui/accordion';
import { Badge } from '@/components/ui/badge';
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from '@/components/ui/breadcrumb';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card';
import {
  ChartContainer,
  ChartLegend,
  ChartLegendContent,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from '@/components/ui/chart';
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from '@/components/ui/command';
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { HoverCard, HoverCardContent, HoverCardTrigger } from '@/components/ui/hover-card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { Progress } from '@/components/ui/progress';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Separator } from '@/components/ui/separator';
import {
  Sheet,
  SheetClose,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/sheet';
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
} from '@/components/ui/sidebar';
import { Skeleton } from '@/components/ui/skeleton';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Textarea } from '@/components/ui/textarea';
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { SAMPLE_BY_PAGE, SAMPLE_FINDINGS, SAMPLE_TOTALS, type SampleFinding } from './sample-data';
import type { ArchitectResult } from '@/api/client';
import { ArchitectLine, ArchitectPairs } from '@/components/results/architect-line';

const ALL_OUTCOMES: Outcome[] = ['PASS', 'FAIL', 'REVIEW_REQUIRED', 'NOT_FOUND', 'NO_APPLICABLE_RULE'];

function Section({
  id,
  title,
  lead,
  children,
}: {
  id: string;
  title: string;
  lead: string;
  children: React.ReactNode;
}) {
  return (
    <section id={id} aria-labelledby={`${id}-title`} className="scroll-mt-20">
      <h2 id={`${id}-title`} className="text-xl font-semibold tracking-tight">
        {title}
      </h2>
      <p className="mt-1 max-w-prose text-sm text-muted-foreground">{lead}</p>
      <div className="mt-5 flex flex-col gap-6">{children}</div>
    </section>
  );
}

function Specimen({ name, children, className }: { name: string; children: React.ReactNode; className?: string }) {
  return (
    <div className={cn('flex flex-col gap-2', className)}>
      <span className="text-xs text-muted-foreground">{name}</span>
      <div className="flex flex-wrap items-center gap-3">{children}</div>
    </div>
  );
}

/* ── Outcomes ─────────────────────────────────────────────────────────────── */

function OutcomePanel({ theme }: { theme: 'light' | 'dark' }) {
  return (
    <div
      data-theme={theme}
      className="flex flex-col gap-3 rounded-xl border bg-background p-4 text-foreground"
    >
      <span className="text-xs text-muted-foreground">{theme === 'light' ? 'Light' : 'Dark'}</span>
      <div className="flex flex-wrap gap-2">
        {ALL_OUTCOMES.map((outcome) => (
          <OutcomeBadge key={outcome} outcome={outcome} />
        ))}
      </div>
      <div className="flex flex-wrap gap-4 text-sm">
        {ALL_OUTCOMES.slice(0, 4).map((outcome) => (
          <span key={outcome} className="inline-flex items-center gap-1.5">
            <OutcomeIcon outcome={outcome} size={16} className={iconTone(outcome)} />
            <span className="num">{SAMPLE_TOTALS.find((t) => t.outcome === outcome)?.count}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

function iconTone(outcome: Outcome): string {
  switch (outcome) {
    case 'PASS':
      return 'text-outcome-pass-fg';
    case 'FAIL':
      return 'text-outcome-fail-fg';
    case 'REVIEW_REQUIRED':
      return 'text-outcome-review-fg';
    default:
      return 'text-outcome-missing-fg';
  }
}

export function OutcomesSection() {
  return (
    <Section
      id="outcomes"
      title="Outcomes"
      lead="Each result always shows its shape and its word; colour only makes it faster to find. Filled means it needs correcting, dashed means a value is still missing."
    >
      <div className="grid gap-4 md:grid-cols-2">
        <OutcomePanel theme="light" />
        <OutcomePanel theme="dark" />
      </div>
    </Section>
  );
}

/* ── Summary: KPI cards + charts ──────────────────────────────────────────── */

const donutConfig = {
  count: { label: 'Checks' },
  PASS: { label: OUTCOME_LABELS.PASS, color: OUTCOME_FILL.PASS },
  FAIL: { label: OUTCOME_LABELS.FAIL, color: OUTCOME_FILL.FAIL },
  REVIEW_REQUIRED: { label: OUTCOME_LABELS.REVIEW_REQUIRED, color: OUTCOME_FILL.REVIEW_REQUIRED },
  NOT_FOUND: { label: OUTCOME_LABELS.NOT_FOUND, color: OUTCOME_FILL.NOT_FOUND },
} satisfies ChartConfig;

const barConfig = {
  pass: { label: OUTCOME_LABELS.PASS, color: OUTCOME_FILL.PASS },
  fail: { label: OUTCOME_LABELS.FAIL, color: OUTCOME_FILL.FAIL },
  review: { label: OUTCOME_LABELS.REVIEW_REQUIRED, color: OUTCOME_FILL.REVIEW_REQUIRED },
  missing: { label: OUTCOME_LABELS.NOT_FOUND, color: OUTCOME_FILL.NOT_FOUND },
} satisfies ChartConfig;

/** Legend in the same order as the stacked bars, bottom to top. */
const LEGEND_ORDER = ['pass', 'fail', 'review', 'missing'];

function Kpi({ outcome, count, total }: { outcome: Outcome; count: number; total: number }) {
  return (
    <Card className="gap-3 py-4">
      <CardHeader className="px-4">
        <CardDescription className="flex items-center gap-1.5">
          <OutcomeIcon outcome={outcome} size={14} className={iconTone(outcome)} />
          {OUTCOME_LABELS[outcome]}
        </CardDescription>
        <CardTitle className="num text-3xl font-medium">{count}</CardTitle>
      </CardHeader>
      <CardContent className="px-4">
        <div className="h-1.5 overflow-hidden rounded-full bg-muted" aria-hidden="true">
          <div className="h-full rounded-full" style={{ width: `${(count / total) * 100}%`, background: OUTCOME_FILL[outcome] }} />
        </div>
        <p className="mt-1.5 text-xs text-muted-foreground">
          <span className="num">{Math.round((count / total) * 100)}%</span> of <span className="num">{total}</span> checks
        </p>
      </CardContent>
    </Card>
  );
}

export function SummarySection() {
  const total = SAMPLE_TOTALS.reduce((sum, t) => sum + t.count, 0);
  const decided = SAMPLE_TOTALS.filter((t) => t.outcome === 'PASS' || t.outcome === 'FAIL').reduce((s, t) => s + t.count, 0);
  const donutData = SAMPLE_TOTALS.map((t) => ({ ...t, fill: OUTCOME_FILL[t.outcome] }));

  return (
    <Section
      id="summary"
      title="Summary"
      lead="A review at a glance: how many checks landed where, and on which pages."
    >
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {SAMPLE_TOTALS.map((t) => (
          <Kpi key={t.outcome} outcome={t.outcome} count={t.count} total={total} />
        ))}
      </div>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
        <Card>
          <CardHeader>
            <CardTitle>Results</CardTitle>
            <CardDescription>
              <span className="num">{decided}</span> of <span className="num">{total}</span> decided by the checks
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ChartContainer config={donutConfig} className="mx-auto aspect-square max-h-60">
              <PieChart>
                <ChartTooltip content={<ChartTooltipContent nameKey="outcome" hideLabel />} />
                <Pie data={donutData} dataKey="count" nameKey="outcome" innerRadius={62} strokeWidth={3} stroke="var(--card)">
                  {donutData.map((d) => (
                    <Cell key={d.outcome} fill={d.fill} />
                  ))}
                  <ChartLabel
                    content={({ viewBox }) => {
                      if (!viewBox || !('cx' in viewBox)) return null;
                      return (
                        <text x={viewBox.cx} y={viewBox.cy} textAnchor="middle" dominantBaseline="middle">
                          <tspan x={viewBox.cx} y={viewBox.cy} className="fill-foreground num text-3xl">
                            {total}
                          </tspan>
                          <tspan x={viewBox.cx} y={(viewBox.cy ?? 0) + 22} className="fill-muted-foreground text-xs">
                            checks
                          </tspan>
                        </text>
                      );
                    }}
                  />
                </Pie>
              </PieChart>
            </ChartContainer>
          </CardContent>
          <CardFooter>
            <Button variant="outline" size="sm" className="w-full">
              <span>
                Open the <span className="num">{total - decided}</span> that need you
              </span>
            </Button>
          </CardFooter>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>By page</CardTitle>
            <CardDescription>Checks on each drawing page</CardDescription>
          </CardHeader>
          <CardContent>
            <ChartContainer config={barConfig} className="aspect-auto h-60 w-full">
              <BarChart data={SAMPLE_BY_PAGE} margin={{ left: -16 }}>
                <CartesianGrid vertical={false} />
                <XAxis dataKey="page" tickLine={false} axisLine={false} tickMargin={8} />
                <YAxis allowDecimals={false} tickLine={false} axisLine={false} width={40} />
                <ChartTooltip content={<ChartTooltipContent />} />
                <ChartLegend content={<ChartLegendContent />} itemSorter={(item) => LEGEND_ORDER.indexOf(String(item.dataKey))} />
                <Bar dataKey="pass" stackId="a" fill={OUTCOME_FILL.PASS} />
                <Bar dataKey="fail" stackId="a" fill={OUTCOME_FILL.FAIL} />
                <Bar dataKey="review" stackId="a" fill={OUTCOME_FILL.REVIEW_REQUIRED} />
                <Bar dataKey="missing" stackId="a" fill={OUTCOME_FILL.NOT_FOUND} radius={[4, 4, 0, 0]} />
              </BarChart>
            </ChartContainer>
          </CardContent>
        </Card>
      </div>
    </Section>
  );
}

/* ── Data table ───────────────────────────────────────────────────────────── */

const FINDING_COLUMNS: ColumnDef<SampleFinding>[] = [
  {
    accessorKey: 'page',
    header: ({ column }) => <SortableHeader column={column}>Page</SortableHeader>,
    cell: ({ row }) => <span className="num">{row.original.page}</span>,
  },
  {
    accessorKey: 'check',
    header: ({ column }) => <SortableHeader column={column}>Check</SortableHeader>,
    cell: ({ row }) => (
      <div className="flex flex-col">
        <span className="font-medium">{row.original.check}</span>
        <span className="text-xs text-muted-foreground">{row.original.scope}</span>
      </div>
    ),
  },
  {
    accessorKey: 'drawn',
    header: 'On the drawing',
    cell: ({ row }) => <span className="num">{row.original.drawn}</span>,
  },
  {
    accessorKey: 'expected',
    header: 'Expected',
    cell: ({ row }) => <span className="num">{row.original.expected}</span>,
  },
  {
    accessorKey: 'outcome',
    header: ({ column }) => <SortableHeader column={column}>Result</SortableHeader>,
    cell: ({ row }) => <OutcomeBadge outcome={row.original.outcome} />,
    sortingFn: (a, b) => outcomeRank(a.original.outcome) - outcomeRank(b.original.outcome),
  },
];

function outcomeRank(outcome: Outcome): number {
  return ['FAIL', 'REVIEW_REQUIRED', 'NOT_FOUND', 'NO_APPLICABLE_RULE', 'PASS'].indexOf(outcome);
}

/* ── Matches the architect (#1085) ─────────────────────────────────────────── */

const sampleValue = (numerator: string, display: string) => ({ numerator, denominator: '1', display });
function sampleArchitect(extra: Partial<ArchitectResult>): ArchitectResult {
  return {
    outcome: 'PASS', finding_id: 'sample', reason: null, needs_decision: false, not_compared_reason: null,
    pairing_source: 'code+ais', pairing_judgments: 'code and both AIs',
    compared: [{
      kind: 'overall', vendor_piece: null, vendor: sampleValue('84', '84"'), architect: sampleValue('84', '84"'), delta: sampleValue('0', '0"'),
      vendor_display: '84"', architect_display: '84"', delta_display: '0"', outcome: 'PASS', architect_location: null,
    }],
    ...extra,
  };
}
const FAIL_PAIRS: ArchitectResult['compared'] = [
  { kind: 'overall', vendor_piece: null, vendor: sampleValue('88', '88"'), architect: sampleValue('84', '84"'), delta: sampleValue('4', '+4"'), vendor_display: '88"', architect_display: '84"', delta_display: '+4"', outcome: 'FAIL', architect_location: null },
  { kind: 'piece', vendor_piece: 2, vendor: sampleValue('30', '30"'), architect: sampleValue('30', '30"'), delta: sampleValue('0', '0"'), vendor_display: '30"', architect_display: '30"', delta_display: '0"', outcome: 'PASS', architect_location: null },
];
// As the API sends a one-judgment result: the pair waits too, so its numbers carry no verdict.
const WAITING_PAIRS: ArchitectResult['compared'] = [
  { kind: 'overall', vendor_piece: null, vendor: sampleValue('88', '88"'), architect: sampleValue('84', '84"'), delta: sampleValue('4', '+4"'), vendor_display: '88"', architect_display: '84"', delta_display: '+4"', outcome: 'REVIEW_REQUIRED', architect_location: null },
];
const ARCHITECT_SAMPLES: { name: string; result: ArchitectResult }[] = [
  { name: 'Not compared', result: sampleArchitect({ outcome: null, finding_id: null, compared: [], pairing_source: null, pairing_judgments: null, not_compared_reason: 'the architect prints only sink centre lines here' }) },
  { name: 'PASS, two judgments', result: sampleArchitect({}) },
  { name: 'FAIL, two judgments', result: sampleArchitect({ outcome: 'FAIL', compared: FAIL_PAIRS }) },
  { name: 'One judgment: code only', result: sampleArchitect({ outcome: 'REVIEW_REQUIRED', needs_decision: true, pairing_source: 'code', pairing_judgments: 'code only', compared: WAITING_PAIRS }) },
  { name: 'One judgment: both AIs only', result: sampleArchitect({ outcome: 'REVIEW_REQUIRED', needs_decision: true, pairing_source: 'both-ais', pairing_judgments: 'both AIs only', compared: WAITING_PAIRS }) },
  { name: 'A reviewer paired it', result: sampleArchitect({ pairing_source: 'reviewer', pairing_judgments: 'reviewer' }) },
];

export function ArchitectSection() {
  return (
    <Section
      id="architect"
      title="Matches the architect"
      lead="The architect line under a countertop, in each state. Not compared is grey words, never a chip. One judgment asks the reviewer to confirm the pairing."
    >
      <div className="flex flex-col gap-3 rounded-xl border bg-card p-4">
        {ARCHITECT_SAMPLES.map((sample) => (
          <div key={sample.name} className="flex flex-col gap-1 border-b pb-3 last:border-b-0 last:pb-0">
            <span className="text-xs text-muted-foreground">{sample.name}</span>
            <ArchitectLine result={sample.result} />
          </div>
        ))}
        <div className="flex flex-col gap-1">
          <span className="text-xs text-muted-foreground">Every compared pair (row details)</span>
          <ArchitectPairs result={sampleArchitect({ outcome: 'FAIL', compared: FAIL_PAIRS })} />
        </div>
      </div>
    </Section>
  );
}

export function DataSection() {
  return (
    <Section
      id="table"
      title="Table"
      lead="Sortable columns, one filter, dimensions in the number face so they line up. Paging stays with the API."
    >
      <DataTable
        columns={FINDING_COLUMNS}
        data={SAMPLE_FINDINGS}
        getRowId={(row) => row.id}
        filterPlaceholder="Filter checks"
        emptyMessage="No checks have run yet."
      />
    </Section>
  );
}

/* ── Controls ─────────────────────────────────────────────────────────────── */

export function ControlsSection() {
  const [layout, setLayout] = useState('three-walls');

  return (
    <Section id="controls" title="Controls" lead="Buttons say what they do. One primary action per view.">
      <Specimen name="Buttons">
        <Button>Sign off</Button>
        <Button variant="secondary">Run checks</Button>
        <Button variant="outline">Show on drawing</Button>
        <Button variant="ghost">Dismiss</Button>
        <Button variant="link">How checks work</Button>
        <Button variant="destructive">Withdraw</Button>
        <Button disabled>Sign off</Button>
      </Specimen>
      <Specimen name="Sizes and icons">
        <Button size="sm">
          <FilePlus2 /> New review
        </Button>
        <Button size="lg">
          <Ruler /> Measure
        </Button>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button size="icon" variant="outline" aria-label="Settings">
              <Settings2 />
            </Button>
          </TooltipTrigger>
          <TooltipContent>Company settings</TooltipContent>
        </Tooltip>
      </Specimen>
      <Specimen name="Badges">
        <Badge>Signed off</Badge>
        <Badge variant="secondary">Revision 2</Badge>
        <Badge variant="outline">Countertop</Badge>
        <Badge variant="destructive">Withdrawn</Badge>
      </Specimen>

      <div className="grid gap-5 sm:grid-cols-2">
        <div className="flex flex-col gap-2">
          <Label htmlFor="kit-width">Countertop width</Label>
          <Input id="kit-width" className="num" placeholder={'e.g. 61 1/4"'} />
          <p className="text-xs text-muted-foreground">Type the value with its unit.</p>
        </div>
        <div className="flex flex-col gap-2">
          <Label htmlFor="kit-layout">Wall layout</Label>
          <Select value={layout} onValueChange={setLayout}>
            <SelectTrigger id="kit-layout" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="three-walls">Walls on both ends</SelectItem>
              <SelectItem value="one-wall">Wall on one end</SelectItem>
              <SelectItem value="no-walls">Free-standing</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="flex flex-col gap-2 sm:col-span-2">
          <Label htmlFor="kit-note">Note for the record</Label>
          <Textarea id="kit-note" placeholder="Why this is acceptable" />
        </div>
      </div>

      <Specimen name="Toggle group">
        <ToggleGroup type="single" variant="outline" defaultValue="needs-me" aria-label="Show">
          <ToggleGroupItem value="needs-me">Needs me</ToggleGroupItem>
          <ToggleGroupItem value="all">All results</ToggleGroupItem>
        </ToggleGroup>
      </Specimen>

      <Specimen name="Progress" className="max-w-sm">
        <div className="flex w-full flex-col gap-1.5">
          <Progress value={62} aria-label="Reading progress" />
          <span className="text-xs text-muted-foreground">
            Reading page <span className="num">11</span> of <span className="num">17</span>
          </span>
        </div>
      </Specimen>
    </Section>
  );
}

/* ── Navigation and disclosure ────────────────────────────────────────────── */

export function DisclosureSection() {
  return (
    <Section
      id="disclosure"
      title="Navigation and disclosure"
      lead="Where you are, and detail only when asked for."
    >
      <Breadcrumb>
        <BreadcrumbList>
          <BreadcrumbItem>
            <BreadcrumbLink href="#/ui-kit">Documents</BreadcrumbLink>
          </BreadcrumbItem>
          <BreadcrumbSeparator />
          <BreadcrumbItem>
            <BreadcrumbLink href="#/ui-kit">Sample vendor</BreadcrumbLink>
          </BreadcrumbItem>
          <BreadcrumbSeparator />
          <BreadcrumbItem>
            <BreadcrumbPage>Page 2</BreadcrumbPage>
          </BreadcrumbItem>
        </BreadcrumbList>
      </Breadcrumb>

      <Separator />

      <Tabs defaultValue="results" className="max-w-xl">
        <TabsList>
          <TabsTrigger value="results">Results</TabsTrigger>
          <TabsTrigger value="measure">Measurements</TabsTrigger>
          <TabsTrigger value="chat">Chat</TabsTrigger>
        </TabsList>
        <TabsContent value="results" className="text-sm text-muted-foreground">
          Every check, one row each, worst first.
        </TabsContent>
        <TabsContent value="measure" className="text-sm text-muted-foreground">
          The values the checks used, and where each came from.
        </TabsContent>
        <TabsContent value="chat" className="text-sm text-muted-foreground">
          Ask about this review in plain words.
        </TabsContent>
      </Tabs>

      <Accordion type="single" collapsible className="max-w-xl">
        <AccordionItem value="why">
          <AccordionTrigger>Why does this need my decision?</AccordionTrigger>
          <AccordionContent className="text-muted-foreground">
            Both readers agreed on the pieces, but the drawing does not show where the walls are.
          </AccordionContent>
        </AccordionItem>
        <AccordionItem value="numbers">
          <AccordionTrigger>Which numbers were used?</AccordionTrigger>
          <AccordionContent>
            <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-1">
              <dt className="text-muted-foreground">Overall</dt>
              <dd className="num">84 1/2"</dd>
              <dt className="text-muted-foreground">Pieces</dt>
              <dd className="num">30" + 24" + 30 1/2"</dd>
              <dt className="text-muted-foreground">Field cut</dt>
              <dd className="num">1" per wall end</dd>
            </dl>
          </AccordionContent>
        </AccordionItem>
      </Accordion>

      <ScrollArea className="h-36 max-w-xl rounded-lg border">
        <ul className="divide-y text-sm">
          {Array.from({ length: 12 }, (_, i) => (
            <li key={i} className="flex items-center justify-between px-3 py-2">
              <span>Page {i + 1}</span>
              <span className="num text-muted-foreground">{(i * 7) % 5} checks</span>
            </li>
          ))}
        </ul>
      </ScrollArea>
    </Section>
  );
}

/* ── Overlays ─────────────────────────────────────────────────────────────── */

export function OverlaysSection() {
  const [showPassed, setShowPassed] = useState(true);
  const sample = SAMPLE_FINDINGS[0];

  return (
    <Section id="overlays" title="Overlays" lead="Confirmations, side panels and menus. Escape closes each one.">
      <div className="flex flex-wrap items-center gap-3">
        <Dialog>
          <DialogTrigger asChild>
            <Button>Sign off</Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Sign off this review?</DialogTitle>
              <DialogDescription>
                The signed report lists every result and every decision you recorded. You cannot change it afterwards.
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <DialogClose asChild>
                <Button variant="outline">Keep reviewing</Button>
              </DialogClose>
              <DialogClose asChild>
                <Button onClick={() => toast.success('Review signed off')}>Sign off</Button>
              </DialogClose>
            </DialogFooter>
          </DialogContent>
        </Dialog>

        <Sheet>
          <SheetTrigger asChild>
            <Button variant="outline">
              <FileText /> Open result
            </Button>
          </SheetTrigger>
          <SheetContent className="w-full sm:max-w-md">
            <SheetHeader>
              <SheetTitle>{sample.scope}</SheetTitle>
              <SheetDescription>
                {sample.check} on page <span className="num">{sample.page}</span>
              </SheetDescription>
            </SheetHeader>
            <div className="flex flex-col gap-4 px-4">
              <OutcomeBadge outcome={sample.outcome} />
              <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-2 text-sm">
                <dt className="text-muted-foreground">On the drawing</dt>
                <dd className="num">{sample.drawn}</dd>
                <dt className="text-muted-foreground">Expected</dt>
                <dd className="num">{sample.expected}</dd>
                <dt className="text-muted-foreground">Difference</dt>
                <dd className="num">−1"</dd>
              </dl>
            </div>
            <SheetFooter>
              <SheetClose asChild>
                <Button onClick={() => toast('Decision saved', { description: 'Marked as needs correction.' })}>
                  Confirm the problem
                </Button>
              </SheetClose>
            </SheetFooter>
          </SheetContent>
        </Sheet>

        <Popover>
          <PopoverTrigger asChild>
            <Button variant="outline">Field cut</Button>
          </PopoverTrigger>
          <PopoverContent className="w-64 text-sm">
            <p className="font-medium">Field cut</p>
            <p className="mt-1 text-muted-foreground">
              Extra stone left on at the factory and trimmed on site: <span className="num">1"</span> per wall end.
            </p>
          </PopoverContent>
        </Popover>

        <HoverCard>
          <HoverCardTrigger asChild>
            <Button variant="link" className="px-0">
              <Info /> Who decided?
            </Button>
          </HoverCardTrigger>
          <HoverCardContent className="text-sm">
            Automatic: both readers read the same numbers and the arithmetic decided. No person changed it.
          </HoverCardContent>
        </HoverCard>

        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="icon" aria-label="More">
              <MoreHorizontal />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start">
            <DropdownMenuLabel>Results</DropdownMenuLabel>
            <DropdownMenuSeparator />
            <DropdownMenuCheckboxItem checked={showPassed} onCheckedChange={(v) => setShowPassed(v === true)}>
              Show results that look right
            </DropdownMenuCheckboxItem>
            <DropdownMenuItem>Download findings PDF</DropdownMenuItem>
            <DropdownMenuItem>Download workbook</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>

        <Button variant="secondary" onClick={() => toast.success('Decision saved')}>
          Show a toast
        </Button>
      </div>

      <Command className="max-w-md rounded-lg border">
        <CommandInput placeholder="Go to a page or check" />
        <CommandList>
          <CommandEmpty>Nothing matches.</CommandEmpty>
          <CommandGroup heading="Pages">
            {[2, 3, 4].map((p) => (
              <CommandItem key={p}>
                <FileText /> Page <span className="num">{p}</span>
              </CommandItem>
            ))}
          </CommandGroup>
          <CommandGroup heading="Checks">
            <CommandItem>
              <Ruler /> Countertop width
            </CommandItem>
            <CommandItem>
              <Ruler /> Sink front offset
            </CommandItem>
          </CommandGroup>
        </CommandList>
      </Command>
    </Section>
  );
}

/* ── Sidebar ──────────────────────────────────────────────────────────────── */

export function SidebarSection() {
  return (
    <Section
      id="sidebar"
      title="Sidebar"
      lead="The app's navigation, shown here in a frame. In the app it collapses to icons and becomes a drawer on phones."
    >
      <div className="h-[360px] overflow-hidden rounded-xl border">
        <SidebarProvider className="min-h-0 h-full">
          <Sidebar collapsible="none" className="h-full border-r">
            <SidebarHeader>
              <Button variant="outline" className="justify-start">
                <FilePlus2 /> New review
              </Button>
            </SidebarHeader>
            <SidebarContent>
              <SidebarGroup>
                <SidebarGroupContent>
                  <SidebarMenu>
                    <SidebarMenuItem>
                      <SidebarMenuButton isActive>
                        <FileText /> Documents
                      </SidebarMenuButton>
                    </SidebarMenuItem>
                    <SidebarMenuItem>
                      <SidebarMenuButton>
                        <BookOpen /> Rulebook
                      </SidebarMenuButton>
                    </SidebarMenuItem>
                  </SidebarMenu>
                </SidebarGroupContent>
              </SidebarGroup>
              <SidebarGroup>
                <SidebarGroupLabel>Yesterday</SidebarGroupLabel>
                <SidebarGroupContent>
                  <SidebarMenu>
                    {['Sample vendor A', 'Sample vendor B'].map((name, i) => (
                      <SidebarMenuItem key={name}>
                        <SidebarMenuButton>{name}</SidebarMenuButton>
                        <SidebarMenuBadge className="num">{i === 0 ? 3 : 0}</SidebarMenuBadge>
                      </SidebarMenuItem>
                    ))}
                  </SidebarMenu>
                </SidebarGroupContent>
              </SidebarGroup>
            </SidebarContent>
            <SidebarFooter>
              <SidebarMenu>
                <SidebarMenuItem>
                  <SidebarMenuButton>
                    <Settings2 /> Company settings <ChevronDown className="ml-auto" />
                  </SidebarMenuButton>
                </SidebarMenuItem>
              </SidebarMenu>
            </SidebarFooter>
          </Sidebar>
          <div className="flex flex-1 items-center justify-center p-6 text-sm text-muted-foreground">
            The review opens here.
          </div>
        </SidebarProvider>
      </div>
    </Section>
  );
}

/* ── Loading ──────────────────────────────────────────────────────────────── */

export function LoadingSection() {
  return (
    <Section id="loading" title="Loading" lead="Placeholders keep the layout still while data arrives; they never stand in for an answer.">
      <div className="flex max-w-md flex-col gap-3">
        <div className="flex items-center gap-3">
          <Skeleton className="size-10 rounded-full" />
          <div className="flex flex-1 flex-col gap-2">
            <Skeleton className="h-4 w-3/4" />
            <Skeleton className="h-4 w-1/2" />
          </div>
        </div>
        <Skeleton className="h-24 w-full" />
      </div>
    </Section>
  );
}

/* ── Type and colour ──────────────────────────────────────────────────────── */

const SWATCHES: { name: string; token: string }[] = [
  { name: 'Background', token: '--background' },
  { name: 'Card', token: '--card' },
  { name: 'Muted', token: '--muted' },
  { name: 'Accent', token: '--accent' },
  { name: 'Border', token: '--border' },
  { name: 'Input edge', token: '--input' },
  { name: 'Muted text', token: '--muted-foreground' },
  { name: 'Foreground', token: '--foreground' },
  { name: 'Pass', token: '--outcome-pass' },
  { name: 'Fail', token: '--outcome-fail' },
  { name: 'Review', token: '--outcome-review' },
  { name: 'Missing', token: '--outcome-missing' },
];

function SwatchPanel({ theme }: { theme: 'light' | 'dark' }) {
  return (
    <div data-theme={theme} className="rounded-xl border bg-background p-4 text-foreground">
      <span className="text-xs text-muted-foreground">{theme === 'light' ? 'Light' : 'Dark'}</span>
      <ul className="mt-3 grid grid-cols-3 gap-3 sm:grid-cols-4">
        {SWATCHES.map((s) => (
          <li key={s.token} className="flex flex-col gap-1">
            <span className="h-10 rounded-md border" style={{ background: `var(${s.token})` }} />
            <span className="text-xs">{s.name}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function FoundationsSection() {
  return (
    <Section
      id="foundations"
      title="Type and colour"
      lead="Words in a plain sans. Numbers, dimensions and ids in IBM Plex Mono with even-width digits, so 1 and l, 0 and O never blur and columns line up."
    >
      <div className="grid gap-4 md:grid-cols-2">
        <div className="flex flex-col gap-2 rounded-xl border p-4">
          <span className="text-xs text-muted-foreground">Words</span>
          <p className="text-2xl font-semibold tracking-tight">Countertop width</p>
          <p className="text-sm text-muted-foreground">The stone must cover the cabinets plus the field cut.</p>
        </div>
        <div className="flex flex-col gap-2 rounded-xl border p-4">
          <span className="text-xs text-muted-foreground">Numbers</span>
          <p className="num text-2xl">1'-0 1/2"  ·  25 1/4"  ·  648 mm</p>
          <p className="num text-sm text-muted-foreground">CT-WIDTH-001 · rev 2 · 0/O 1/l/I</p>
        </div>
      </div>
      <div className="grid gap-4 md:grid-cols-2">
        <SwatchPanel theme="light" />
        <SwatchPanel theme="dark" />
      </div>
    </Section>
  );
}

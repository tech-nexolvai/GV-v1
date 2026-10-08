import { useEffect, useRef, useState } from 'react';
import { BarChart2, BookOpen, Files, Moon, SlidersHorizontal, SquarePen, Sun } from 'lucide-react';

import { getApprovalReadiness, listPackages } from '@/api/client';
import type { PackagePage } from '@/api/client';
import { projectId } from '@/api/config';
import { useAsync } from '@/api/useAsync';
import type { Page } from '@/app/route';
import type { Theme } from '@/app/theme';
import { PackageStatusBadge } from '@/components/ui/package-status-badge';
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarMenuSkeleton,
  useSidebar,
} from '@/components/ui/sidebar';

type PackageItem = PackagePage['items'][number];

const PAGE_SIZE = 50;

/** Package states in which someone may still need to decide something. */
const IN_REVIEW = new Set(['AWAITING_REVIEW', 'NEEDS_INPUT', 'CHANGES_REQUESTED']);

const PRIMARY_NAV: { id: Page; label: string; icon: typeof Files }[] = [
  { id: 'documents', label: 'Documents', icon: Files },
  { id: 'rulebook', label: 'Rulebook', icon: BookOpen },
];

const FOOTER_NAV: { id: Page; label: string; icon: typeof Files }[] = [
  // GV's standard numbers, set once for every project (#812).
  { id: 'settings', label: 'Company settings', icon: SlidersHorizontal },
  { id: 'usage', label: 'Usage', icon: BarChart2 },
];

export interface AppSidebarProps {
  activePage: Page;
  activePackage: string | null;
  theme: Theme;
  /** Bumped after an upload or a sign-off, so the list reloads without a page refresh. */
  refreshKey: number;
  /** The open review's own, newest "need you" count, which wins over the list's copy. */
  liveNeedYou: { packageId: string; count: number } | null;
  onNavigate: (page: Page) => void;
  onOpenPackage: (packageId: string) => void;
  onNewReview: () => void;
  onToggleTheme: () => void;
}

/**
 * The app's navigation on shadcn's Sidebar (#1034): the same items in the same order as before,
 * collapsible to icons (remembered by the shell) and a sheet on phones.
 *
 * The reviews list is the project's **packages**, not its review sessions: a package is what a
 * reviewer calls "a review", and the review screen finds its open sitting itself.
 */
export function AppSidebar({
  activePage,
  activePackage,
  theme,
  refreshKey,
  liveNeedYou,
  onNavigate,
  onOpenPackage,
  onNewReview,
  onToggleTheme,
}: AppSidebarProps) {
  const { isMobile, setOpenMobile } = useSidebar();
  const first = useAsync(() => listPackages(projectId(), { limit: PAGE_SIZE }), [refreshKey]);
  // Older pages, appended when the reviewer asks for them. Dropped whenever the first page reloads.
  const [more, setMore] = useState<{ items: PackageItem[]; cursor: string | null } | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<string | null>(null);
  const pageGeneration = useRef(0);

  useEffect(() => {
    pageGeneration.current += 1;
    // A fresh first page makes appended pages stale; drop them rather than show duplicates.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setMore(null);
    setLoadingMore(false);
    setMoreError(null);
  }, [refreshKey]);

  // A refreshed first page can overlap an older cursor page: keep one entry per package.
  const seen = new Set<string>();
  const items = first.status === 'ready'
    ? [...first.data.items, ...(more?.items ?? [])].filter((item) => {
        if (seen.has(item.id)) return false;
        seen.add(item.id);
        return true;
      })
    : [];
  const nextCursor = more ? more.cursor : first.status === 'ready' ? first.data.next_cursor ?? null : null;
  const needYou = useNeedYouCounts(items, refreshKey);

  async function loadMore() {
    if (!nextCursor || loadingMore) return;
    const generation = pageGeneration.current;
    setLoadingMore(true);
    setMoreError(null);
    try {
      const page = await listPackages(projectId(), { cursor: nextCursor, limit: PAGE_SIZE });
      if (generation !== pageGeneration.current) return;
      setMore((current) => ({ items: [...(current?.items ?? []), ...page.items], cursor: page.next_cursor ?? null }));
    } catch (error) {
      if (generation !== pageGeneration.current) return;
      setMoreError(error instanceof Error ? error.message : String(error));
    } finally {
      if (generation === pageGeneration.current) setLoadingMore(false);
    }
  }

  /** Anything chosen on a phone also closes the sheet, so the reviewer lands on what they picked. */
  function closing<A extends unknown[]>(action: (...args: A) => void) {
    return (...args: A) => {
      if (isMobile) setOpenMobile(false);
      action(...args);
    };
  }

  const isNewReview = activePage === 'review' && activePackage === null;
  const ThemeIcon = theme === 'dark' ? Sun : Moon;
  const themeLabel = theme === 'dark' ? 'Light theme' : 'Dark theme';

  return (
    <Sidebar collapsible="icon" aria-label="Reviews and navigation" className="font-sans">
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton isActive={isNewReview} aria-current={isNewReview ? 'page' : undefined} tooltip="New review" onClick={closing(onNewReview)}>
              <SquarePen />
              <span>New review</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
          {PRIMARY_NAV.map(({ id, label, icon: Icon }) => (
            <SidebarMenuItem key={id}>
              <SidebarMenuButton isActive={activePage === id} aria-current={activePage === id ? 'page' : undefined} tooltip={label} onClick={closing(() => onNavigate(id))}>
                <Icon />
                <span>{label}</span>
              </SidebarMenuButton>
            </SidebarMenuItem>
          ))}
        </SidebarMenu>
      </SidebarHeader>

      <SidebarContent className="group-data-[collapsible=icon]:hidden">
        {first.status === 'loading' && (
          <SidebarGroup aria-label="Loading reviews">
            <SidebarMenu>
              {[0, 1, 2].map((key) => (
                <SidebarMenuItem key={key}>
                  <SidebarMenuSkeleton />
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroup>
        )}

        {first.status === 'error' && (
          /* Named, not shown as an empty list: "no reviews" is not the same as "we could not ask". */
          <p className="px-4 py-2 text-xs text-muted-foreground" role="alert">
            Could not load reviews — {first.error.message}
          </p>
        )}

        {first.status === 'ready' && items.length === 0 && (
          <p className="px-4 py-2 text-xs text-muted-foreground">No reviews yet. Start one with New review.</p>
        )}

        {groupByDay(items).map(({ label, items: group }) => (
          <SidebarGroup key={label} aria-label={label}>
            <SidebarGroupLabel>{label}</SidebarGroupLabel>
            <SidebarGroupContent>
              <SidebarMenu>
                {group.map((pkg) => {
                  const active = activePage === 'review' && activePackage === pkg.id;
                  const count = liveNeedYou?.packageId === pkg.id ? liveNeedYou.count : needYou.get(pkg.id);
                  return (
                    <SidebarMenuItem key={pkg.id}>
                      <SidebarMenuButton
                        size="lg"
                        isActive={active}
                        aria-current={active ? 'page' : undefined}
                        className="h-auto flex-col items-start gap-1 py-2"
                        onClick={closing(() => onOpenPackage(pkg.id))}
                      >
                        {/* Packages carry only a vendor; there is no human package name yet. */}
                        <span className="w-full truncate font-medium">{pkg.vendor ?? 'Untitled document set'}</span>
                        <span className="flex w-full flex-wrap items-center gap-1.5">
                          <PackageStatusBadge status={pkg.state} />
                          {count !== undefined && count > 0 && (
                            <span className="text-xs text-outcome-review-fg">
                              <span className="num">{count}</span> need you
                            </span>
                          )}
                          {pkg.current_revision_number > 1 && (
                            <span className="text-xs text-muted-foreground">Rev <span className="num">{pkg.current_revision_number}</span></span>
                          )}
                        </span>
                      </SidebarMenuButton>
                    </SidebarMenuItem>
                  );
                })}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        ))}

        {nextCursor && (
          <div className="px-2 pb-2">
            <SidebarMenuButton onClick={() => void loadMore()} disabled={loadingMore} className="text-muted-foreground">
              {loadingMore ? 'Loading…' : 'Show older reviews'}
            </SidebarMenuButton>
          </div>
        )}
        {moreError && (
          <p className="px-4 py-2 text-xs text-muted-foreground" role="alert">
            Could not load older reviews — {moreError}
          </p>
        )}
      </SidebarContent>

      <SidebarFooter className="mt-auto">
        <SidebarMenu>
          {FOOTER_NAV.map(({ id, label, icon: Icon }) => (
            <SidebarMenuItem key={id}>
              <SidebarMenuButton isActive={activePage === id} aria-current={activePage === id ? 'page' : undefined} tooltip={label} onClick={closing(() => onNavigate(id))}>
                <Icon />
                <span>{label}</span>
              </SidebarMenuButton>
            </SidebarMenuItem>
          ))}
          <SidebarMenuItem>
            <SidebarMenuButton tooltip={themeLabel} onClick={onToggleTheme}>
              <ThemeIcon />
              <span>{themeLabel}</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>
    </Sidebar>
  );
}

/**
 * How many findings still need someone, per package under review, from the same readiness answer
 * the review screen uses. A package whose answer failed is simply absent (shown without a count),
 * never 0 — "nothing needs you" is a claim this list may only make from the server.
 */
function useNeedYouCounts(items: readonly PackageItem[], refreshKey: number): Map<string, number> {
  const [counts, setCounts] = useState<Map<string, number>>(new Map());
  const wanted = items.filter((item) => IN_REVIEW.has(item.state)).map((item) => item.id).join(',');

  useEffect(() => {
    if (!wanted) return;
    let current = true;
    const ids = wanted.split(',');
    void Promise.allSettled(ids.map((id) => getApprovalReadiness(projectId(), id))).then((answers) => {
      if (!current) return;
      const next = new Map<string, number>();
      answers.forEach((answer, index) => {
        if (answer.status === 'fulfilled') next.set(ids[index], answer.value.blocking_findings);
      });
      setCounts(next);
    });
    return () => {
      current = false;
    };
  }, [wanted, refreshKey]);

  return counts;
}

/** Today / Yesterday / Previous 7 days / Previous 30 days / then by month — newest first. */
function groupByDay(items: readonly PackageItem[]): { label: string; items: PackageItem[] }[] {
  const startOfToday = new Date();
  startOfToday.setHours(0, 0, 0, 0);
  const day = 24 * 60 * 60 * 1000;

  const groups: { label: string; items: PackageItem[] }[] = [];
  for (const item of items) {
    const created = new Date(item.created_at);
    const startOfThatDay = new Date(created);
    startOfThatDay.setHours(0, 0, 0, 0);
    const age = startOfToday.getTime() - startOfThatDay.getTime();
    const label =
      age <= 0 ? 'Today'
      : age <= day ? 'Yesterday'
      : age <= 7 * day ? 'Previous 7 days'
      : age <= 30 * day ? 'Previous 30 days'
      : created.toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
    const last = groups[groups.length - 1];
    if (last && last.label === label) last.items.push(item);
    else groups.push({ label, items: [item] });
  }
  return groups;
}

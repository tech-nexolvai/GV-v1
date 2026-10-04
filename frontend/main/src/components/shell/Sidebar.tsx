import { useEffect, useState } from 'react';
import {
  BookOpen,
  BarChart2,
  Files,
  Moon,
  PanelLeft,
  SlidersHorizontal,
  SquarePen,
  Sun,
  X,
} from 'lucide-react';
import { listPackages } from '../../api/client';
import type { PackagePage } from '../../api/client';
import { projectId } from '../../api/config';
import { useAsync } from '../../api/useAsync';
import type { Page } from '../../app/route';
import type { Theme } from '../../app/theme';
import { StatusBadge } from '../ui/Badge';
import './Sidebar.css';

type PackageItem = PackagePage['items'][number];

interface SidebarProps {
  collapsed: boolean;
  /** Under 768px the sidebar is a drawer over the page rather than a column beside it. */
  mobileOpen: boolean;
  activePage: Page;
  activePackage: string | null;
  theme: Theme;
  /** Bumped after an upload so the new review appears in the list without a reload. */
  refreshKey: number;
  onToggleCollapsed: () => void;
  onCloseMobile: () => void;
  onNavigate: (page: Page) => void;
  onOpenPackage: (packageId: string) => void;
  onNewReview: () => void;
  onToggleTheme: () => void;
}

const PAGE_SIZE = 50;

const PRIMARY_NAV: { id: Page; label: string; icon: typeof Files }[] = [
  { id: 'documents', label: 'Documents', icon: Files },
  { id: 'rulebook',  label: 'Rulebook',  icon: BookOpen },
];

const FOOTER_NAV: { id: Page; label: string; icon: typeof Files }[] = [
  // GV's standard numbers, set once for every project (#812).
  { id: 'settings', label: 'Company settings', icon: SlidersHorizontal },
  { id: 'usage',    label: 'Usage',            icon: BarChart2 },
];

/**
 * The reviews list is the project's **packages**, not its review sessions.
 *
 * It listed review sessions before and passed a session id to a screen that loads by package id,
 * so every entry opened a "could not be loaded" page. A package is what a reviewer thinks of as "a
 * review": one vendor's document set with its findings. Sessions are the record of a sitting inside
 * one, and the review screen finds the open one itself.
 */
export function Sidebar({
  collapsed,
  mobileOpen,
  activePage,
  activePackage,
  theme,
  refreshKey,
  onToggleCollapsed,
  onCloseMobile,
  onNavigate,
  onOpenPackage,
  onNewReview,
  onToggleTheme,
}: SidebarProps) {
  const first = useAsync(() => listPackages(projectId(), { limit: PAGE_SIZE }), [refreshKey]);
  // Older pages, appended when the reviewer asks for them. Dropped whenever the first page reloads.
  const [more, setMore] = useState<{ items: PackageItem[]; cursor: string | null } | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<string | null>(null);

  useEffect(() => {
    // A fresh first page makes appended pages stale; drop them rather than show duplicates.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setMore(null);
  }, [refreshKey]);

  // A refreshed first page can overlap an older cursor page. Keep the newest copy of each
  // package so React never renders two navigation entries with the same identity.
  const seenPackageIds = new Set<string>();
  const items = first.status === 'ready'
    ? [...first.data.items, ...(more?.items ?? [])].filter((item) => {
        if (seenPackageIds.has(item.id)) return false;
        seenPackageIds.add(item.id);
        return true;
      })
    : [];
  const nextCursor = more
    ? more.cursor
    : first.status === 'ready'
      ? first.data.next_cursor ?? null
      : null;

  async function loadMore() {
    if (!nextCursor || loadingMore) return;
    setLoadingMore(true);
    setMoreError(null);
    try {
      const page = await listPackages(projectId(), { cursor: nextCursor, limit: PAGE_SIZE });
      setMore((current) => ({
        items: [...(current?.items ?? []), ...page.items],
        cursor: page.next_cursor ?? null,
      }));
    } catch (error) {
      setMoreError(error instanceof Error ? error.message : String(error));
    } finally {
      setLoadingMore(false);
    }
  }

  const isNewReview = activePage === 'review' && activePackage === null;
  const ThemeIcon = theme === 'dark' ? Sun : Moon;
  const themeLabel = theme === 'dark' ? 'Light theme' : 'Dark theme';
  const rail = collapsed && !mobileOpen;

  return (
    <aside
      className="sidebar"
      data-collapsed={rail}
      data-mobile-open={mobileOpen}
      aria-label="Reviews and navigation"
    >
      <div className="sidebar__top">
        <button
          type="button"
          className="sidebar__icon-btn sidebar__toggle"
          onClick={onToggleCollapsed}
          aria-label={collapsed ? 'Open sidebar' : 'Close sidebar'}
          title={collapsed ? 'Open sidebar' : 'Close sidebar'}
        >
          <PanelLeft size={18} />
        </button>
        <button
          type="button"
          className="sidebar__icon-btn sidebar__close"
          onClick={onCloseMobile}
          aria-label="Close menu"
        >
          <X size={18} />
        </button>
      </div>

      <nav className="sidebar__nav" aria-label="Primary">
        <button
          type="button"
          className="sidebar__item"
          data-active={isNewReview}
          aria-current={isNewReview ? 'page' : undefined}
          onClick={onNewReview}
          aria-label="New review"
          title={rail ? 'New review' : undefined}
        >
          <SquarePen size={17} className="sidebar__item-icon" />
          <span className="sidebar__item-label">New review</span>
        </button>
        {PRIMARY_NAV.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            type="button"
            className="sidebar__item"
            data-active={activePage === id}
            aria-current={activePage === id ? 'page' : undefined}
            onClick={() => onNavigate(id)}
            aria-label={label}
            title={rail ? label : undefined}
          >
            <Icon size={17} className="sidebar__item-icon" />
            <span className="sidebar__item-label">{label}</span>
          </button>
        ))}
      </nav>

      <div className="sidebar__reviews" hidden={rail}>
        {first.status === 'loading' && (
          <div className="sidebar__group" aria-label="Loading reviews">
            {[70, 52, 61].map((width) => (
              <span key={width} className="skeleton sidebar__skeleton" style={{ width: `${width}%` }} />
            ))}
          </div>
        )}

        {first.status === 'error' && (
          /* Named, not shown as an empty list. "No reviews" is a different and more comfortable
             statement than "we could not ask". */
          <p className="sidebar__state" role="alert">
            Could not load reviews — {first.error.message}
          </p>
        )}

        {first.status === 'ready' && items.length === 0 && (
          <p className="sidebar__state">No reviews yet. Start one with New review.</p>
        )}

        {groupByDay(items).map(({ label, items: group }) => (
          <section key={label} className="sidebar__group" aria-label={label}>
            <h2 className="sidebar__group-label">{label}</h2>
            <ul className="sidebar__list">
              {group.map((pkg) => {
                const active = activePage === 'review' && activePackage === pkg.id;
                return (
                  <li key={pkg.id}>
                    <button
                      type="button"
                      className="sidebar__review"
                      data-active={active}
                      aria-current={active ? 'page' : undefined}
                      onClick={() => onOpenPackage(pkg.id)}
                    >
                      <span className="sidebar__review-title">
                        {/* Packages carry only a vendor; there is no human package name yet. */}
                        {pkg.vendor ?? 'Untitled document set'}
                      </span>
                      <span className="sidebar__review-meta">
                        <StatusBadge status={pkg.state} size="sm" />
                        {pkg.current_revision_number > 1 && (
                          <span className="sidebar__review-rev">Rev {pkg.current_revision_number}</span>
                        )}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </section>
        ))}

        {nextCursor && (
          <button
            type="button"
            className="sidebar__more"
            onClick={() => void loadMore()}
            disabled={loadingMore}
          >
            {loadingMore ? 'Loading…' : 'Show older reviews'}
          </button>
        )}
        {moreError && (
          <p className="sidebar__state" role="alert">
            Could not load older reviews — {moreError}
          </p>
        )}
      </div>

      <div className="sidebar__footer">
        {FOOTER_NAV.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            type="button"
            className="sidebar__item"
            data-active={activePage === id}
            aria-current={activePage === id ? 'page' : undefined}
            onClick={() => onNavigate(id)}
            aria-label={label}
            title={rail ? label : undefined}
          >
            <Icon size={17} className="sidebar__item-icon" />
            <span className="sidebar__item-label">{label}</span>
          </button>
        ))}
        <button
          type="button"
          className="sidebar__item"
          onClick={onToggleTheme}
          aria-label={themeLabel}
          title={rail ? themeLabel : undefined}
        >
          <ThemeIcon size={17} className="sidebar__item-icon" />
          <span className="sidebar__item-label">{themeLabel}</span>
        </button>
      </div>
    </aside>
  );
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

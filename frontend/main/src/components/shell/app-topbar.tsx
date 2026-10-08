import { Fragment } from 'react';

import { GVMark } from '@/components/brand/GVMark';
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from '@/components/ui/breadcrumb';
import { Separator } from '@/components/ui/separator';
import { SidebarTrigger } from '@/components/ui/sidebar';

export interface Crumb {
  label: string;
  /** Where the crumb goes; the last crumb (the screen itself) has none. */
  onSelect?: () => void;
}

/**
 * The header (#1034): where you are on the left (breadcrumb, then whatever the screen places beside
 * it — status, project values), Graniti Vicentia on the true centre line as the client's brief asks,
 * and the screen's own actions on the right. The two slots are filled by `ShellHeader` portals, so
 * each screen keeps owning its controls and their handlers.
 */
export function AppTopbar({
  title,
  crumbs,
  titleRef,
  actionsRef,
}: {
  /** The screen's name, for assistive technology and the window title. */
  title: string;
  crumbs: Crumb[];
  titleRef: (element: HTMLDivElement | null) => void;
  actionsRef: (element: HTMLDivElement | null) => void;
}) {
  return (
    <header
      data-tw
      className="grid h-14 shrink-0 grid-cols-[minmax(0,1fr)_auto] items-center sm:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] gap-2 border-b bg-background px-2 font-sans text-foreground sm:gap-3 sm:px-3"
    >
      <div className="flex min-w-0 items-center gap-1.5 sm:gap-2">
        <SidebarTrigger aria-label="Toggle sidebar" />
        <Separator orientation="vertical" className="mr-1 hidden data-[orientation=vertical]:h-4 sm:block" />
        <h1 className="sr-only">{title}</h1>
        <Breadcrumb className="min-w-0">
          <BreadcrumbList className="flex-nowrap">
            {crumbs.map((crumb, index) => {
              const last = index === crumbs.length - 1;
              return (
                <Fragment key={`${index}-${crumb.label}`}>
                  <BreadcrumbItem className={last ? 'min-w-0' : 'hidden shrink-0 md:inline-flex'}>
                    {last || !crumb.onSelect ? (
                      <BreadcrumbPage className="truncate font-medium">{crumb.label}</BreadcrumbPage>
                    ) : (
                      <BreadcrumbLink asChild>
                        <button type="button" onClick={crumb.onSelect} className="whitespace-nowrap">
                          {crumb.label}
                        </button>
                      </BreadcrumbLink>
                    )}
                  </BreadcrumbItem>
                  {!last && <BreadcrumbSeparator className="hidden md:inline-flex" />}
                </Fragment>
              );
            })}
          </BreadcrumbList>
        </Breadcrumb>
        <div ref={titleRef} className="hidden min-w-0 items-center gap-2 empty:hidden sm:flex" />
      </div>

      {/* Under 640px there is no room for a centre column: the screen's action needs the space. */}
      <div className="hidden items-center gap-2 sm:flex" aria-label="Graniti Vicentia">
        <GVMark size={24} />
        <span className="hidden text-xs font-semibold tracking-widest uppercase lg:inline">Graniti Vicentia</span>
      </div>

      <div ref={actionsRef} className="flex min-w-0 items-center justify-end gap-2" />
    </header>
  );
}

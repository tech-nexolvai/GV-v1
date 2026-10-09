import { ArrowLeft, Moon, Sun } from 'lucide-react';

import { useTheme } from '@/app/theme';
import { Toaster } from '@/components/ui/sonner';
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group';
import { TooltipProvider } from '@/components/ui/tooltip';
import {
  ControlsSection,
  DataSection,
  DisclosureSection,
  FoundationsSection,
  LoadingSection,
  OutcomesSection,
  ArchitectSection,
  ResultsNotesSection,
  OverlaysSection,
  SidebarSection,
  SummarySection,
} from './sections';
import { AssistantSection } from './assistant-section';

const SECTIONS = [
  { id: 'outcomes', title: 'Outcomes' },
  { id: 'summary', title: 'Summary' },
  { id: 'table', title: 'Table' },
  { id: 'architect', title: 'Matches the architect' },
  { id: 'results-notes', title: 'Results: said once' },
  { id: 'assistant', title: 'Assistant' },
  { id: 'controls', title: 'Controls' },
  { id: 'disclosure', title: 'Navigation and disclosure' },
  { id: 'overlays', title: 'Overlays' },
  { id: 'sidebar', title: 'Sidebar' },
  { id: 'loading', title: 'Loading' },
  { id: 'foundations', title: 'Type and colour' },
] as const;

/**
 * The reference for the redesign (#1029): every shadcn/ui primitive the reviewer screens will be
 * built from, in this app's tokens, with made-up data. Reached at `#/ui-kit`; not in the sidebar.
 *
 * `data-tw` opts the page into Tailwind's reset (src/styles/preflight-scoped.css), which the legacy
 * pages never get.
 */
export function UiKitPage() {
  const [theme, toggleTheme] = useTheme();

  return (
    <TooltipProvider delayDuration={200}>
      <div data-tw className="h-full overflow-y-auto bg-background font-sans text-foreground antialiased">
        <header className="sticky top-0 z-20 border-b bg-background/90 backdrop-blur">
          <div className="mx-auto flex h-14 max-w-6xl items-center gap-3 px-4 sm:px-6">
            <a
              href="#/"
              className="inline-flex size-8 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
              aria-label="Back to GV Review"
            >
              <ArrowLeft className="size-4" />
            </a>
            <h1 className="text-base font-semibold">UI kit</h1>
            <p className="hidden text-sm text-muted-foreground sm:block">
              The building blocks of the reviewer screens. All data on this page is made up.
            </p>
            <ToggleGroup
              type="single"
              variant="outline"
              size="sm"
              className="ml-auto"
              value={theme}
              onValueChange={(next) => {
                if (next && next !== theme) toggleTheme();
              }}
              aria-label="Theme"
            >
              <ToggleGroupItem value="light" aria-label="Light theme">
                <Sun /> <span className="hidden sm:inline">Light</span>
              </ToggleGroupItem>
              <ToggleGroupItem value="dark" aria-label="Dark theme">
                <Moon /> <span className="hidden sm:inline">Dark</span>
              </ToggleGroupItem>
            </ToggleGroup>
          </div>
        </header>

        <div className="mx-auto grid max-w-6xl gap-10 px-4 py-8 sm:px-6 lg:grid-cols-[180px_minmax(0,1fr)]">
          <nav aria-label="Sections" className="hidden lg:block">
            <ul className="sticky top-22 flex flex-col gap-0.5 text-sm">
              {SECTIONS.map((section) => (
                <li key={section.id}>
                  <a
                    href={`#/ui-kit`}
                    onClick={(event) => {
                      // The hash is the app's route, so scroll by id instead of navigating.
                      event.preventDefault();
                      document.getElementById(section.id)?.scrollIntoView({ block: 'start' });
                    }}
                    className="block rounded-md px-2 py-1.5 text-muted-foreground hover:bg-accent hover:text-foreground"
                  >
                    {section.title}
                  </a>
                </li>
              ))}
            </ul>
          </nav>

          <main className="flex min-w-0 flex-col gap-14 pb-24">
            <OutcomesSection />
            <SummarySection />
            <DataSection />
            <ArchitectSection />
            <ResultsNotesSection />
            <AssistantSection />
            <ControlsSection />
            <DisclosureSection />
            <OverlaysSection />
            <SidebarSection />
            <LoadingSection />
            <FoundationsSection />
          </main>
        </div>
        <Toaster position="bottom-right" />
      </div>
    </TooltipProvider>
  );
}

export default UiKitPage;

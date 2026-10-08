import { Download, FileCheck2, Loader2, MoreHorizontal, PenLine, Play, ScanText, SearchCheck } from 'lucide-react';

import type { NextAction, NextActionKind } from '@/lib/review-stage';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';

export interface SecondaryAction {
  id: string;
  label: string;
  onSelect: () => void;
  disabled?: boolean;
}

const ICON: Partial<Record<NextActionKind, typeof Play>> = {
  'wait-reading': ScanText,
  'run-checks': Play,
  checking: Loader2,
  review: SearchCheck,
  'sign-off': PenLine,
  'preparing-report': Loader2,
  'prepare-report': FileCheck2,
  'download-report': Download,
};

/**
 * The one primary action for where the review stands, and a small menu for everything else.
 *
 * The words and whether it can be pressed come from `reviewStage()`; `extraDisabled` lets the page
 * add its own in-flight guards (a sign-off already being sent) without the label changing. A
 * disabled action always says why, beside it on hover and to screen readers.
 */
export function NextActionButton({
  action,
  onAct,
  busyLabel,
  extraDisabled = false,
  secondary = [],
}: {
  action: NextAction;
  onAct: (kind: NextActionKind) => void;
  /** Replaces the label while the page is doing the action (e.g. "Signing off…"). */
  busyLabel?: string | null;
  extraDisabled?: boolean;
  secondary?: SecondaryAction[];
}) {
  const Icon = ICON[action.kind];
  const disabled = action.disabled || extraDisabled;
  const spinning = action.kind === 'checking' || action.kind === 'preparing-report' || Boolean(busyLabel);
  const reasonId = 'next-action-reason';

  const button = (
    <Button
      data-slot="next-action"
      data-kind={action.kind}
      size="sm"
      disabled={disabled}
      aria-describedby={disabled && action.reason ? reasonId : undefined}
      onClick={() => onAct(action.kind)}
    >
      {Icon && <Icon className={spinning ? 'animate-spin motion-reduce:animate-none' : undefined} aria-hidden="true" />}
      {busyLabel ?? action.label}
    </Button>
  );

  return (
    <div className="flex items-center gap-1.5" data-slot="next-action-group">
      {disabled && action.reason ? (
        <Tooltip>
          {/* A disabled button gets no pointer events, so the tooltip hangs on a wrapper. */}
          <TooltipTrigger asChild>
            <span tabIndex={0} className="rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring">{button}</span>
          </TooltipTrigger>
          <TooltipContent side="bottom" className="max-w-72">{action.reason}</TooltipContent>
        </Tooltip>
      ) : button}
      {disabled && action.reason && <span id={reasonId} className="sr-only">{action.reason}</span>}
      {secondary.length > 0 && (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="icon-sm" aria-label="More actions">
              <MoreHorizontal />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-52">
            {secondary.map((item) => (
              <DropdownMenuItem key={item.id} disabled={item.disabled} onSelect={item.onSelect}>
                {item.label}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  );
}

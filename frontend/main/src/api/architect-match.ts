/**
 * TEMPORARY, HAND-WRITTEN (#1168): the separate architect PDF's view match, as the interface contract
 * (section C) describes it. The backend for it (Phases 3 to 5) is being built in parallel, so these
 * shapes are not in `schema.d.ts` yet.
 *
 * REPLACE every type here with the generated one (`components['schemas'][...]`) once the backend is
 * merged and `npm run api:types` has regenerated `schema.d.ts`, then delete this file. Every field the
 * server adds is optional or nullable here, so a combined-sheet set (all of them null) reads exactly
 * as today.
 */
import type { components } from './schema';

type RowLocation = components['schemas']['RowLocation'];

/** Where a countertop's architect view match stands (`workflow/architect_match_contract.MatchStatus`). */
export type ArchitectMatchStatus =
  /** An older run: the architect file is read, but this countertop was never matched with a view. */
  | 'not_matched_yet'
  /** Code and the AIs did not agree on one view: the reviewer picks, nothing pre-selected. */
  | 'needs_reviewer'
  /** Code (a printed reference or a clear geometry fit) and both AIs picked the same view. */
  | 'auto_matched'
  /** A reviewer picked the view. */
  | 'reviewer_confirmed'
  /** The same view as the reviewer's (or the automatic) match on the revision before. */
  | 'carried_over'
  /** The reviewer said no view in the architect's file shows this countertop. */
  | 'none_matches'
  /** The matched view runs into its neighbour, so its dimensions were not read. */
  | 'not_separated'
  /** The architect's file has no view to match with. */
  | 'no_candidates';

/** One view of the architect's file (`ArchitectViewRefOut`). */
export interface ArchitectViewRef {
  view_id: string;
  document_id: string;
  document_version_id: string;
  file_name: string;
  /** Counted from 1, as the file's own page. */
  page_number: number;
  sheet_number: string | null;
  bubble: string | null;
  title: string | null;
  scale_note: string | null;
  /** The server's own short name for the view, e.g. "arch.pdf, page 2, view 3". */
  label: string;
  /** The view's frame on its page, in the same stored 0–1 space as `row_location`. */
  region: RowLocation | null;
  picture_url: string | null;
  separated: boolean;
}

/** What one AI answered when asked which view shows the countertop. */
export interface ArchitectAiPick {
  model_label: string;
  answer: 'view' | 'none' | 'unsure' | 'no_answer';
  view_id: string | null;
  why: string;
}

/** A countertop's view match (`ArchitectMatchOut`), as the results carry it. */
export interface ArchitectMatch {
  record_id: string | null;
  status: ArchitectMatchStatus;
  source: 'automatic' | 'reviewer' | 'carried' | null;
  /** Whose judgments the match rests on, in words ("code and both AIs", "reviewer", …). */
  judgments: string | null;
  code_verdict: string | null;
  code_pick_view_id: string | null;
  ai_picks: ArchitectAiPick[];
  matched_view: ArchitectViewRef | null;
  needs_decision: boolean;
  reason: string | null;
  /**
   * A reviewer's pick recorded after the result on screen: it counts once the checks run again.
   * Addition to the contract (#1168): the server compares the pick's time with the live run's.
   */
  waits_for_run?: boolean;
}

/** The new fields on `ArchitectResultOut`; all null (or absent) on a combined-sheet set. */
export interface ArchitectMatchFields {
  match?: ArchitectMatch | null;
  compared_with?: ArchitectViewRef | null;
  compared_with_text?: string | null;
}

/** One candidate view in the reviewer's picker, ranked by code. */
export interface ArchitectViewCandidate {
  rank: number;
  view: ArchitectViewRef;
  shown_to_ais: boolean;
  code: {
    fits: boolean;
    reference_match: boolean;
    run_length_error_display: string | null;
    bays_vendor: number | null;
    bays_architect: number | null;
    pair_support: number | null;
  };
  /** One line: why code ranked it here. */
  score_summary: string;
  evidence: string[];
  /** The AIs that picked this view, by their labels. */
  ai_picked_by: string[];
  /** The reviewer chose this view for the same countertop on an earlier revision (never pre-selected). */
  remembered: boolean;
  can_pick: boolean;
  refusal: string | null;
}

/** `GET …/slot-rows/{row_id}/architect-view-match` (`ArchitectViewMatchOut`). */
export interface ArchitectViewMatch {
  row_id: string;
  vendor: {
    page_number: number;
    document_version_id: string;
    title: string | null;
    references: string[];
    region: RowLocation | null;
  };
  current: {
    record_id: string;
    status: ArchitectMatchStatus;
    source: 'automatic' | 'reviewer' | 'carried';
    decided_by: string | null;
    decided_at: string | null;
    supersedes_id: string | null;
    note: string | null;
  } | null;
  candidates: ArchitectViewCandidate[];
  can_choose_none: boolean;
}

/** `POST …/architect-view-match` (`ArchitectViewPickIn`): exactly one of `view_id` / `none_of_these`. */
export interface ArchitectViewPickIn {
  view_id: string | null;
  none_of_these: boolean;
  note: string | null;
  expected_record_id: string | null;
}

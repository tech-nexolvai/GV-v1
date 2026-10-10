/**
 * The separate architect PDF's view match (#1168).
 *
 * The countertop results' shapes are generated (`schema.d.ts`). The reviewer picker's request and
 * answer (`GET/POST …/architect-view-match`, #1166) are still HAND-WRITTEN from the interface
 * contract, because #1166's backend is built in parallel: replace them with the generated types once
 * it is merged and `npm run api:types` has regenerated `schema.d.ts`.
 */
import type { components } from './schema';

type RowLocation = components['schemas']['RowLocation'];

// Generated from the countertop results (#1168, `app/schemas/visual_ui.py`).
export type ArchitectMatchStatus = components['schemas']['ArchitectMatchOut']['status'];
export type ArchitectViewRef = components['schemas']['ArchitectViewRefOut'];
export type ArchitectAiPick = components['schemas']['ArchitectAiPickOut'];
export type ArchitectMatch = components['schemas']['ArchitectMatchOut'];

// ── TEMPORARY, HAND-WRITTEN: the reviewer picker's API (#1166) is not merged yet. ──
// Replace these three with `components['schemas']['ArchitectViewMatchOut']`, its candidates, and
// `ArchitectViewPickIn` once #1166 is merged and `npm run api:types` has run.

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
    decided_at: string;
    supersedes_id: string | null;
    note: string | null;
    reasons: string[];
  } | null;
  candidates: ArchitectViewCandidate[];
  can_choose_none: boolean;
}

/** `POST …/architect-view-match` (`ArchitectViewPickIn`): exactly one of `view_id` / `none_of_these`. */
export interface ArchitectViewPickIn {
  view_id: string | null;
  none_of_these: boolean;
  note: string | null;
  expected_record_id: string;
}

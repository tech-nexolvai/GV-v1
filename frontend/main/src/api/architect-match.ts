/**
 * The separate architect PDF's view match (#1166, #1168), as the API describes it. Every type is
 * generated (`schema.d.ts`); these are only short names for them.
 */
import type { components } from './schema';

type Schemas = components['schemas'];

/** A countertop's match, as the countertop results carry it (`app/schemas/visual_ui.py`). */
export type ArchitectMatch = Schemas['ArchitectMatchOut'];
export type ArchitectMatchStatus = ArchitectMatch['status'];
export type ArchitectAiPick = Schemas['ArchitectAiPickOut'];
/** One view of the architect's own file (`app/schemas/architect_matches.py`). */
export type ArchitectViewRef = Schemas['ArchitectViewRefOut'];
/** The reviewer picker (`GET/POST …/slot-rows/{row_id}/architect-view-match`). */
export type ArchitectViewMatch = Schemas['ArchitectViewMatchOut'];
export type ArchitectViewCandidate = Schemas['ArchitectViewCandidateOut'];
export type ArchitectViewPickIn = Schemas['ArchitectViewPickIn'];

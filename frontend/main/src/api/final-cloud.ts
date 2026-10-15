/**
 * The final cloud PDF from the simple review page (#1368): the existing sign-off, then (a separate
 * step) the existing signed files, of which the cloud PDF is the drawing redline. Nothing new on the server: these are
 * the advanced view's own calls (`approvePackage`, `prepareSignedExports`, `getSignedExports`,
 * `downloadRedline`), gathered behind one interface so the page can be tested with its own.
 *
 * Whether the review may be signed is never worked out here: the server's `can_finalise` unlocks
 * the button, and `approve` refuses with the reason when it no longer holds.
 */
import {
  approvePackage,
  downloadRedline,
  getCountertopResults,
  getPackage,
  getSignedExports,
  listReviewSessions,
  openReviewSession,
  prepareSignedExports,
} from './client';
import { carriedNoCountertopConfirmations, carriedWallsDrivingResults, type CarriedWall } from '@/lib/revisions';

/** Where the signed files are: not asked for yet, being made, ready to download, or failed. */
export type SignedFilesStatus = 'not_requested' | 'preparing' | 'ready' | 'failed';

/** What the sign-off confirmation names: who signs, and any answers carried from an earlier revision. */
export interface SignOffFacts {
  /** The signed-in reviewer as the server's own sittings name them; null when none does yet. */
  signer: string | null;
  carried: CarriedWall[];
}

export interface FinalCloudApi {
  /** Who signs and what was carried over, read when the confirmation opens. */
  facts: (projectId: string, packageId: string) => Promise<SignOffFacts>;
  /** Sign off (the reviewer's own open sitting on this revision, opened when there is none).
   * Resolves once the sign-off is recorded; the signed files are asked for separately (`prepare`),
   * so a failure there is never mistaken for "nothing was signed". Refused by the server, with its
   * reason, when the review is not ready. */
  signOff: (projectId: string, packageId: string, carriedConfirmed: boolean) => Promise<void>;
  /** Ask for the signed files of the existing sign-off (right after it, or after a failure). Never re-signs. */
  prepare: (projectId: string, packageId: string) => Promise<SignedFilesStatus>;
  status: (projectId: string, packageId: string) => Promise<SignedFilesStatus>;
  /** The final cloud PDF (the signed drawing redline). */
  download: (projectId: string, packageId: string) => Promise<Blob>;
}

export const liveFinalCloudApi: FinalCloudApi = {
  async facts(projectId, packageId) {
    const [sessions, results] = await Promise.all([
      listReviewSessions(projectId),
      getCountertopResults(projectId, packageId).catch(() => null),
    ]);
    return {
      signer: sessions.items[0]?.reviewer ?? null,
      carried: results
        ? [...carriedWallsDrivingResults(results.items), ...carriedNoCountertopConfirmations(results.pages_without_countertop ?? [])]
        : [],
    };
  },
  async signOff(projectId, packageId, carriedConfirmed) {
    const detail = await getPackage(projectId, packageId);
    const sessions = await listReviewSessions(projectId);
    const open = sessions.items.find(
      (item) => item.package_revision_id === detail.current_revision_id && item.completed_at === null,
    );
    const sitting = open ?? (await openReviewSession(projectId, packageId, detail.current_revision_id));
    await approvePackage(projectId, sitting.id, carriedConfirmed);
  },
  async prepare(projectId, packageId) {
    return (await prepareSignedExports(projectId, packageId)).status;
  },
  async status(projectId, packageId) {
    return (await getSignedExports(projectId, packageId)).status;
  },
  download: (projectId, packageId) => downloadRedline(projectId, packageId),
};

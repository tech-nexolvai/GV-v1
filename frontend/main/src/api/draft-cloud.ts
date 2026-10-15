/**
 * The draft cloud PDF from the simple review page (#1394): the W4b routes (#1340). Ask for it (202),
 * ask where it stands every few seconds while the worker makes it, then fetch the file. A draft is
 * never the signed file, and asking for one signs nothing.
 */
import { downloadDraftCloud, getDraftCloud, requestDraftCloud, type DraftCloudStatus } from './client';

export type { DraftCloudStatus };

export interface DraftCloudApi {
  /** POST: queue a draft of the current revision, or the request already waiting. */
  start: (projectId: string, packageId: string) => Promise<DraftCloudStatus>;
  /** GET: where the newest request stands. */
  status: (projectId: string, packageId: string) => Promise<DraftCloudStatus>;
  /** The newest draft, once it is ready. */
  download: (projectId: string, packageId: string) => Promise<Blob>;
}

export const liveDraftCloudApi: DraftCloudApi = {
  start: requestDraftCloud,
  status: getDraftCloud,
  download: downloadDraftCloud,
};

/** How often the page asks whether the draft is ready, while the worker makes it. */
export const DRAFT_POLL_MS = 3000;

/**
 * Ask for a draft and wait for it: resolves with the file, or throws with the reason in plain words
 * (the worker's own `reason` when it failed). `stillWanted` false (the page closed, or a newer ask)
 * stops the waiting and resolves null.
 */
export async function makeDraftCloud(
  api: DraftCloudApi,
  projectId: string,
  packageId: string,
  { pollMs = DRAFT_POLL_MS, stillWanted = () => true }: { pollMs?: number; stillWanted?: () => boolean } = {},
): Promise<Blob | null> {
  let answer = await api.start(projectId, packageId);
  while (answer.status === 'preparing') {
    await new Promise((resolve) => setTimeout(resolve, pollMs));
    if (!stillWanted()) return null;
    answer = await api.status(projectId, packageId);
  }
  if (!stillWanted()) return null;
  if (answer.status === 'failed') throw new Error(answer.reason?.trim() || 'The worker could not make it.');
  if (answer.status !== 'ready') throw new Error('The request was not taken. Try again.');
  return api.download(projectId, packageId);
}

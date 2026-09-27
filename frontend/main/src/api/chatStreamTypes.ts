/**
 * Types for the reviewer chat stream, in a module with no runtime imports.
 *
 * Kept apart from `client.ts` (which reads Vite's `import.meta.env`) so pure code and its Node
 * tests can use them. The frame payloads are not in the generated schema: FastAPI describes an SSE
 * response as `text/event-stream` without typing its frames.
 */

import type { paths } from './schema';

export type ReviewerChatReply =
  paths['/api/v1/projects/{project_id}/packages/{package_id}/chat']['post']['responses'][200]['content']['application/json'];

/** Sent first on the chat stream: which recorded findings answer the question. No model involved. */
export interface ChatStreamFacts {
  answer: string;
  finding_ids: string[];
  total: number;
  /** Whether a model is about to write about them. False means the reply that follows is plain. */
  narrating: boolean;
}

export interface ChatStreamStage {
  stage: string;
  model_id: string | null;
}

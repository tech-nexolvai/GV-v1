import type { ChatStreamStage } from '../../api/chatStreamTypes';

/** Unknown stages must not turn into invented pipeline activity. */
export function chatProgress(stage?: ChatStreamStage): { label: string; modelId: string | null } {
  if (stage?.stage === 'narrating') {
    return { label: 'Writing an explanation of recorded findings', modelId: stage.model_id };
  }
  return { label: 'Waiting for the review response', modelId: null };
}

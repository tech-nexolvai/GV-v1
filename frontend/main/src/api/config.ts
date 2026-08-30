/**
 * Which project this client is looking at.
 *
 * Every endpoint is `/projects/{project_id}/…` because project scope is an isolation boundary rather
 * than a filter (ADR-0006): a caller outside a project is told the thing does not exist, since a 403
 * would confirm that it does. So the client cannot make a single call without knowing the project.
 *
 * **This is temporary and deliberately awkward.** The prototype navigates by local state rather than
 * by route, so there is nowhere for a project to live in the URL yet. Reading it from the environment
 * gets a developer running without inventing a project-picker that would then have to be unbuilt.
 * When routing gains `/projects/:projectId/…`, this reads the route param and the variable goes.
 *
 * It **refuses** when unset rather than defaulting. A default would be a real UUID belonging to
 * nobody, and every call would 404 against a boundary doing exactly what it should — which reads as
 * a broken client rather than a missing setting.
 */

export class ProjectNotConfigured extends Error {
  constructor() {
    super(
      'VITE_PROJECT_ID is not set, so this client does not know which project to ask about. ' +
        'Set it in .env.local to a project UUID. It is not defaulted: a made-up project would 404 ' +
        'against a boundary behaving correctly, which looks like a broken API rather than a ' +
        'missing setting.',
    );
    this.name = 'ProjectNotConfigured';
  }
}

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** The configured project id, or a refusal. Never a placeholder. */
export function projectId(): string {
  const configured = import.meta.env.VITE_PROJECT_ID;
  if (!configured || !configured.trim()) throw new ProjectNotConfigured();

  const value = configured.trim();
  if (!UUID_PATTERN.test(value)) {
    // Refused rather than passed through: the server would 404 on it, and a 404 here is
    // indistinguishable from "that project is not yours", which is the answer this boundary gives on
    // purpose. Better to say the setting is malformed than to let it look like a permissions problem.
    throw new Error(
      `VITE_PROJECT_ID is "${value}", which is not a UUID. Every project is identified by one.`,
    );
  }
  return value;
}

/** A package can exist even if a later upload step fails. Never encourage a duplicate submission. */
export class PartialUploadError extends Error {
  readonly packageId: string;
  constructor(packageId: string, cause: unknown) {
    super(cause instanceof Error ? cause.message : String(cause), { cause });
    this.name = 'PartialUploadError';
    this.packageId = packageId;
  }
}

/** A package exists even though one of the later upload or extraction steps failed. */
export class IncompletePackageUpload extends Error {
  readonly packageId: string;

  constructor(packageId: string, cause: unknown) {
    const detail = cause instanceof Error ? cause.message : String(cause);
    super(detail, { cause });
    this.name = 'IncompletePackageUpload';
    this.packageId = packageId;
  }
}

export function describeUploadFailure(failure: unknown): {
  detail: string;
  savedPackageId: string | null;
} {
  return {
    detail: failure instanceof Error ? failure.message : String(failure),
    savedPackageId: failure instanceof IncompletePackageUpload ? failure.packageId : null,
  };
}

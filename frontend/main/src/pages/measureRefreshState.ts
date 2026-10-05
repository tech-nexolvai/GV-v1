/** Whether a Measure refresh changed drawing packages, which is the only reason drafts reset. */
export function packageChanged(
  previousPackageId: string | null | undefined,
  nextPackageId: string | undefined,
): boolean {
  return previousPackageId !== nextPackageId;
}

/** A failed refresh of a loaded package is recoverable; an initial or switched-package load is not. */
export function refreshFailureIsFatal(
  loadedPackageId: string | null,
  selectedPackageId: string | undefined,
): boolean {
  return loadedPackageId === null || loadedPackageId !== selectedPackageId;
}

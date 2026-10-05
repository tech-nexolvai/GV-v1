/** Whether a Measure refresh changed drawing packages, which is the only reason drafts reset. */
export function packageChanged(
  previousPackageId: string | null | undefined,
  nextPackageId: string | undefined,
): boolean {
  return previousPackageId !== nextPackageId;
}

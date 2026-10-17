import type { components } from './schema';

/** A product id of the product registry (#994, #1547), as `GET /products` lists it. */
export type ProductType = string;
/** One product as `GET /products` lists it (inactive ones too). */
export type ProductChoice = components['schemas']['ProductOut'];

/**
 * Whether a drawing set can be started for this product: it must be active (an inactive one is
 * "coming later") and the published rulebook must check it (a set checked against nothing would
 * look exactly like a clean one).
 */
export function canUploadFor(choice: ProductChoice): boolean {
  return choice.active && choice.published_checks > 0;
}

/**
 * The product to preselect: the first one in the registry's order that can be uploaded for, and
 * `null` when there is none — the form then cannot start.
 */
export function defaultProductType(choices: readonly ProductChoice[]): ProductType | null {
  return choices.find(canUploadFor)?.id ?? null;
}

/** The body of `POST /projects/{id}/packages`: who sent the set and what it is for. */
export function packageCreateBody(
  vendor: string,
  productType: ProductType,
): components['schemas']['PackageCreate'] {
  return { vendor: vendor || null, product_type: productType };
}

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

export type DrawingSlot = 'architectural' | 'shop';

/**
 * Which files to upload, as which drawing (#963).
 *
 * The same bytes chosen for both slots are one combined set (ADR-0020): uploaded once, as the shop
 * drawing, and the reviewer confirms whose drawings are on it. Uploaded twice it was read twice, and
 * the slot made the vendor's own drawing the architect's — a vendor-vs-architect check could compare
 * a drawing with itself. `sameBytes` is the caller's comparison of the two files' SHA-256 digests.
 */
export function uploadPlan<T>(
  architectural: T | null | undefined,
  shop: T | null | undefined,
  sameBytes: boolean,
): Array<[T, DrawingSlot]> {
  if (architectural && shop && sameBytes) return [[shop, 'shop']];
  const plan: Array<[T, DrawingSlot]> = [];
  if (architectural) plan.push([architectural, 'architectural']);
  if (shop) plan.push([shop, 'shop']);
  return plan;
}

interface FileIdentity {
  name: string;
  size: number;
  lastModified: number;
}

/** A quick, pre-hash hint for the form's wording; the upload itself compares digests. */
export function looksLikeTheSameFile(a: FileIdentity | null, b: FileIdentity | null): boolean {
  return (
    a !== null && b !== null && a.name === b.name && a.size === b.size && a.lastModified === b.lastModified
  );
}

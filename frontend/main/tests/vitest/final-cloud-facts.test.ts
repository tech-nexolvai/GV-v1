import { describe, expect, it, vi } from 'vitest';

// Synthetic data only: nothing here comes from a client drawing.
vi.mock('@/api/client', () => ({
  getPackage: vi.fn(async () => ({ current_revision_id: 'rev-this' })),
  listReviewSessions: vi.fn(async () => ({
    items: [
      { id: 's-other', package_revision_id: 'rev-other-package', reviewer: 'Other Reviewer', completed_at: null },
      { id: 's-this', package_revision_id: 'rev-this', reviewer: 'Sample Reviewer', completed_at: null },
    ],
  })),
  getCountertopResults: vi.fn(async () => null),
  approvePackage: vi.fn(),
  downloadRedline: vi.fn(),
  getSignedExports: vi.fn(),
  openReviewSession: vi.fn(),
  prepareSignedExports: vi.fn(),
}));

const { liveFinalCloudApi } = await import('@/api/final-cloud');

describe('final cloud sign-off facts (#1394)', () => {
  it("names the signer from this package's own revision, never the newest sitting in the project", async () => {
    const facts = await liveFinalCloudApi.facts('proj-demo', 'pkg-demo');
    expect(facts.signer).toBe('Sample Reviewer');
  });
});

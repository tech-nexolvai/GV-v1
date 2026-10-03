/** Own one image request. Disposed requests cannot publish or retain a blob URL. */
export function loadPassageImage(
  download: () => Promise<Blob>,
  ready: (url: string) => void,
  failed: () => void,
  urls: Pick<typeof URL, 'createObjectURL' | 'revokeObjectURL'> = URL,
): () => void {
  let active = true;
  let objectUrl: string | undefined;
  void Promise.resolve().then(download).then((blob) => {
    if (!active) return;
    objectUrl = urls.createObjectURL(blob);
    ready(objectUrl);
  }).catch(() => { if (active) failed(); });
  return () => {
    active = false;
    if (objectUrl) { urls.revokeObjectURL(objectUrl); objectUrl = undefined; }
  };
}

import { useState } from 'react';
import { getSignedExports, prepareSignedExports } from '../../api/client';
import { useAsync } from '../../api/useAsync';
import { SignedDownloadsView } from './SignedDownloadsView';

export function SignedDownloads({ projectId, packageId, download }: {
  projectId: string;
  packageId: string;
  download: (format: 'pdf' | 'workbook' | 'redline') => void;
}) {
  const [refresh, setRefresh] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [preparing, setPreparing] = useState(false);
  const state = useAsync(() => getSignedExports(projectId, packageId), [projectId, packageId, refresh]);
  async function prepare() {
    setPreparing(true);
    setError(null);
    try {
      await prepareSignedExports(projectId, packageId);
      setRefresh((count) => count + 1);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setPreparing(false);
    }
  }
  return <>
    <SignedDownloadsView status={state.status === 'ready' ? state.data.status : state.status === 'error' ? 'error' : 'loading'} busy={preparing} request={() => void prepare()} refresh={() => setRefresh((count) => count + 1)} download={download} />
    {error && <p role="alert">{error}</p>}
  </>;
}

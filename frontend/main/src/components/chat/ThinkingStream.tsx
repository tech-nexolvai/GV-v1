import { useEffect, useState } from 'react';
import type { ChatStreamStage } from '../../api/chatStreamTypes';
import { chatProgress } from './chatProgress';
import { GVMark } from '../brand/GVMark';
import './ThinkingStream.css';

/** Waiting time is measured; activity comes only from a server event. Chat does not rerun checks. */
export function ThinkingStream({ stage }: { stage?: ChatStreamStage }) {
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    const started = Date.now();
    const timer = window.setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const progress = chatProgress(stage);
  return (
    <div className="thinking" role="status" aria-live="polite">
      <div className="thinking__head">
        <GVMark size={22} />
        <span className="thinking__title">{progress.label}</span>
        <span className="thinking__elapsed mono" aria-hidden="true">{elapsed}s</span>
      </div>
      <div className="thinking__track" aria-hidden="true"><span className="thinking__bar" /></div>
      {progress.modelId && <p className="thinking__model">Provider model: <code>{progress.modelId}</code></p>}
    </div>
  );
}

import { useRef } from 'react';
import * as Dialog from '@radix-ui/react-dialog';

/** Focus stays in the open sheet; Escape closes it and returns focus to its opener. */
export function ModalSheet({ open, onClose, title, className, children }: {
  open: boolean; onClose: () => void; title: string; className: string; children: React.ReactNode;
}) {
  const opener = useRef<HTMLElement | null>(null);
  return (
    <Dialog.Root open={open} onOpenChange={(value) => { if (!value) onClose(); }}>
      <Dialog.Portal>
        <Dialog.Overlay className="shell__modal-overlay" />
        <Dialog.Content className={className} aria-describedby={undefined}
          onOpenAutoFocus={() => { opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null; }}
          onCloseAutoFocus={(event) => { event.preventDefault(); if (opener.current?.isConnected) opener.current.focus(); }}>
          <Dialog.Title className="shell__sr-only">{title}</Dialog.Title>
          {children}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/**
 * Fetching, with the three states a screen actually has.
 *
 * Deliberately not a data-fetching library. The app needs one thing — call this, show a spinner,
 * show an error, show the data — and a library would bring caching and invalidation semantics that
 * nothing here has yet decided it wants.
 *
 * **The error state is not optional.** A hook that returned `data | undefined` would let a page
 * render "no findings" when the call had actually failed, and under a review workflow that reads as
 * *nothing is wrong with this drawing*. Failure and emptiness are different answers and this keeps
 * them apart.
 */

import { useEffect, useState } from 'react';

export type AsyncState<T> =
  | { status: 'loading' }
  | { status: 'error'; error: Error }
  | { status: 'ready'; data: T };

/**
 * Run `load` when `deps` change, and report the outcome.
 *
 * The result of a superseded call is discarded rather than rendered: a slow first request landing
 * after a fast second one would otherwise show the wrong package's findings, which is the kind of
 * mistake nobody notices because the screen looks fine.
 */
export function useAsync<T>(load: () => Promise<T>, deps: readonly unknown[]): AsyncState<T> {
  const [state, setState] = useState<AsyncState<T>>({ status: 'loading' });

  useEffect(() => {
    let current = true;
    setState({ status: 'loading' });

    load().then(
      (data) => {
        if (current) setState({ status: 'ready', data });
      },
      (error: unknown) => {
        if (current) {
          setState({
            status: 'error',
            error: error instanceof Error ? error : new Error(String(error)),
          });
        }
      },
    );

    return () => {
      current = false;
    };
    // `load` is rebuilt every render, so the caller's `deps` are what decide when to refetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return state;
}

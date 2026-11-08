// One place for how the app shows waiting, nothing-yet and failure, and for the request
// bookkeeping every screen needs (latest-request-wins, no double submit).
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  ApiError,
  ApiHttpError,
  ApiNetworkError,
  ApiTimeoutError,
  ApiUnavailableError,
  ModeMismatchError,
  describeError,
  retryAfterOf,
} from "../lib/api";

export type ErrorKind =
  | "input" //            a local message (nothing was sent)
  | "timeout" //          no answer in time
  | "unreachable" //      could not connect
  | "rate_limited" //     429
  | "model_unavailable" // 502
  | "unavailable" //      503 and other server-side outages
  | "validation" //       422
  | "bad_request" //      400
  | "mode_mismatch" //    app and backend disagree on live vs fixture
  | "contract" //         the answer is not what the v1 contract says
  | "unknown";

export interface ErrorView {
  kind: ErrorKind;
  /** Short title; null for plain local messages. */
  headline: string | null;
  /** The detailed line, with the server's code and request id when it sent them. */
  message: string;
  /** What the user can do about it. */
  hint: string;
  /** Whether trying the same thing again can reasonably help. */
  retryable: boolean;
  /** Seconds the server asked the user to wait, if it said so. */
  retryAfterSeconds?: number;
}

/** Maps any error thrown by the API client to what the user should see. Pure. */
export function classifyError(error: unknown): ErrorView {
  if (typeof error === "string") {
    return { kind: "input", headline: null, message: error, hint: "", retryable: false };
  }
  const message = describeError(error);
  const retryAfterSeconds = retryAfterOf(error);
  if (error instanceof ModeMismatchError) {
    return {
      kind: "mode_mismatch",
      headline: "Live and fixture mode disagree",
      message,
      hint: "Switch the app to the mode the backend is running in, or restart the backend in the mode shown in the banner.",
      retryable: false,
    };
  }
  if (error instanceof ApiTimeoutError) {
    return {
      kind: "timeout",
      headline: "The backend is taking too long",
      message,
      hint: "The request may still have been processed on the server. Nothing on this screen was changed.",
      retryable: true,
    };
  }
  if (error instanceof ApiNetworkError) {
    return {
      kind: "unreachable",
      headline: "The backend is not reachable",
      message,
      hint: "Check that the backend is running and that its address and allowed origin are right.",
      retryable: true,
    };
  }
  if (error instanceof ApiError) {
    switch (error.status) {
      case 429:
        return {
          kind: "rate_limited",
          headline: "Too many requests",
          message,
          hint: "The service is limiting requests. Wait a little before trying again.",
          retryable: true,
          retryAfterSeconds,
        };
      case 502:
        return {
          kind: "model_unavailable",
          headline: "The AI model is unavailable",
          message,
          hint: "The language model did not answer. The deterministic calculations are not affected.",
          retryable: true,
        };
      case 503:
        return {
          kind: "unavailable",
          headline: "The service is unavailable",
          message,
          hint: "The backend cannot take requests right now.",
          retryable: true,
          retryAfterSeconds,
        };
      case 422:
        return {
          kind: "validation",
          headline: "The backend rejected the values",
          message,
          hint: "Correct the values named here, then try again. Sending the same values again will not help.",
          retryable: false,
        };
      case 400:
        return {
          kind: "bad_request",
          headline: "The request was malformed",
          message,
          hint: "Sending the same request again will not help. Please report this.",
          retryable: false,
        };
      default:
        return { kind: "unknown", headline: "Unexpected backend error", message, hint: "", retryable: false };
    }
  }
  if (error instanceof ApiHttpError) {
    const transient = [429, 502, 503, 504].includes(error.status);
    return {
      kind: error.status === 429 ? "rate_limited" : transient ? "unavailable" : "unknown",
      headline: error.status === 429 ? "Too many requests" : transient ? "The service is unavailable" : "Unexpected backend error",
      message,
      hint: transient ? "The backend did not send a structured error. Try again in a moment." : "",
      retryable: transient,
      retryAfterSeconds,
    };
  }
  if (error instanceof ApiUnavailableError) {
    return {
      kind: "contract",
      headline: "Unexpected answer from the backend",
      message,
      hint: "The answer does not match the agreed format, so it was not used. Retrying is unlikely to help; please report this.",
      retryable: false,
    };
  }
  return { kind: "unknown", headline: "Something went wrong", message, hint: "", retryable: false };
}

/** "5 s" or "3 min". */
export function formatWait(seconds: number): string {
  return seconds < 120 ? `${seconds} s` : `${Math.ceil(seconds / 60)} min`;
}

/** Whole seconds left of a wait, counting down once a second. Restarts when `key` changes. */
function useCountdown(seconds: number | undefined, key: unknown): number {
  const [left, setLeft] = useState(seconds ?? 0);
  useEffect(() => {
    setLeft(seconds ?? 0);
    if (!seconds) return;
    const end = Date.now() + seconds * 1000;
    const timer = setInterval(() => {
      const remaining = Math.max(0, Math.ceil((end - Date.now()) / 1000));
      setLeft(remaining);
      if (remaining === 0) clearInterval(timer);
    }, 250);
    return () => clearInterval(timer);
  }, [seconds, key]);
  return left;
}

interface AsyncErrorProps {
  error: unknown;
  /** Legacy-style lead-in such as "Extraction failed.". */
  prefix?: string;
  /** Closing reassurance such as "Your inputs are unchanged.". */
  suffix?: string;
  /** Extra line when the AI model is unavailable and the user can do the work by hand. */
  manualHint?: string;
  /** Deliberate retry. Shown only when retrying can help, and disabled while the server's wait runs. */
  onRetry?: () => void;
  testId?: string;
  /**
   * Move keyboard focus to the message when it appears, so the user lands on what went wrong.
   * Turn off for errors nobody asked for (the automatic health check on page load).
   */
  focusOnMount?: boolean;
}

/** The single error view. Always a role="alert"; never replaces or hides what the user typed. */
export function AsyncError({ error, prefix, suffix, manualHint, onRetry, testId, focusOnMount = true }: AsyncErrorProps) {
  const view = classifyError(error);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (focusOnMount) box.current?.focus();
  }, [error, focusOnMount]);
  const left = useCountdown(view.retryAfterSeconds, error);
  const line = [prefix, view.message, suffix].filter(Boolean).join(" ");
  const waiting = left > 0;
  return (
    <div
      ref={box}
      tabIndex={-1}
      role="alert"
      data-testid={testId ?? "async-error"}
      data-kind={view.kind}
      className="mt-2 rounded border border-red-300 bg-red-50 p-2 text-sm text-red-900"
    >
      {view.headline && <p className="font-semibold">{view.headline}</p>}
      <p>{line}</p>
      {view.hint && <p className="mt-1">{view.hint}</p>}
      {view.kind === "model_unavailable" && manualHint && <p className="mt-1">{manualHint}</p>}
      {view.retryAfterSeconds !== undefined && view.retryAfterSeconds > 0 && (
        <p className="mt-1" data-testid="retry-after">
          The service asked for a wait of {formatWait(view.retryAfterSeconds)} before trying again.
        </p>
      )}
      {onRetry && view.retryable && (
        <button
          type="button"
          onClick={() => {
            if (!waiting) onRetry();
          }}
          aria-disabled={waiting}
          className="mt-2 min-h-8 rounded border border-red-700 bg-white px-3 py-1 font-medium text-red-900 aria-disabled:cursor-not-allowed aria-disabled:opacity-60"
        >
          {waiting ? `Try again in ${formatWait(left)}` : "Try again"}
        </button>
      )}
    </div>
  );
}

/** Work in progress. Uses role="status" so it is announced politely, not as an error. */
export function Pending({ children, testId }: { children: ReactNode; testId?: string }) {
  return (
    <p role="status" aria-busy="true" data-testid={testId} className="mt-2 flex items-center gap-2 text-sm text-slate-700">
      <span
        aria-hidden="true"
        className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-slate-400 border-t-transparent motion-reduce:animate-none"
      />
      <span>{children}</span>
    </p>
  );
}

/** "Nothing here yet" with the reason, instead of a blank area. */
export function EmptyState({ children, testId }: { children: ReactNode; testId?: string }) {
  return (
    <p data-testid={testId} className="rounded border border-dashed border-slate-300 bg-slate-50 p-2 text-sm text-slate-700">
      {children}
    </p>
  );
}

export interface LatestRequest {
  id: number;
  signal: AbortSignal;
  /** False once the inputs changed, the user cancelled or a newer request began. */
  isCurrent: () => boolean;
  /** Call in `finally`; frees the slot only if this request still owns it. */
  done: () => void;
}

/**
 * Bookkeeping for one screen's requests. `start()` returns null while a request is already
 * running, so a double click or a double call cannot send twice. `invalidate()` (edits,
 * cancel, mode switch, unmount) aborts the running request and makes its late reply
 * recognisable as stale through `isCurrent()`.
 */
export function useLatestRequest() {
  const seq = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const busy = useRef(false);

  const invalidate = useCallback(() => {
    seq.current += 1;
    controller.current?.abort();
    controller.current = null;
    busy.current = false;
  }, []);

  const start = useCallback((): LatestRequest | null => {
    if (busy.current) return null;
    const next = new AbortController();
    controller.current = next;
    seq.current += 1;
    busy.current = true;
    const id = seq.current;
    return {
      id,
      signal: next.signal,
      isCurrent: () => id === seq.current,
      done: () => {
        if (id === seq.current) busy.current = false;
      },
    };
  }, []);

  useEffect(
    () => () => {
      controller.current?.abort();
    },
    [],
  );

  return { start, invalidate };
}

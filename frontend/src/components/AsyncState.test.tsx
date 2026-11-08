import { act, render, renderHook, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  ApiHttpError,
  ApiNetworkError,
  ApiTimeoutError,
  ApiUnavailableError,
  ModeMismatchError,
} from "../lib/api";
import { AsyncError, EmptyState, Pending, classifyError, formatWait, useLatestRequest } from "./AsyncState";

const envelope = (status: number, code: string, retryAfter?: number) =>
  new ApiError(status, code, `${code} message`, [], "req-1", retryAfter);

afterEach(() => vi.useRealTimers());

describe("classifyError: every documented error code and failure mode", () => {
  it.each([
    [400, "bad_request", false],
    [422, "validation", false],
    [429, "rate_limited", true],
    [502, "model_unavailable", true],
    [503, "unavailable", true],
  ])("HTTP %i -> %s (retryable: %s)", (status, kind, retryable) => {
    const view = classifyError(envelope(status, "SOME_CODE"));
    expect(view.kind).toBe(kind);
    expect(view.retryable).toBe(retryable);
    expect(view.message).toContain("SOME_CODE");
    expect(view.message).toContain("request req-1");
    expect(view.headline).toBeTruthy();
  });

  it("an undocumented status is shown as unexpected and not retried", () => {
    expect(classifyError(envelope(418, "TEAPOT"))).toMatchObject({ kind: "unknown", retryable: false });
  });

  it("timeout, disconnect, non-contract answers and mode mismatch", () => {
    expect(classifyError(new ApiTimeoutError("slow"))).toMatchObject({ kind: "timeout", retryable: true });
    expect(classifyError(new ApiNetworkError("down"))).toMatchObject({ kind: "unreachable", retryable: true });
    expect(classifyError(new ApiUnavailableError("does not match the v1 contract"))).toMatchObject({
      kind: "contract",
      retryable: false,
    });
    expect(classifyError(new ModeMismatchError("mismatch"))).toMatchObject({ kind: "mode_mismatch", retryable: false });
  });

  it("HTTP errors without the v1 envelope are classified by status", () => {
    expect(classifyError(new ApiHttpError("x", 503))).toMatchObject({ kind: "unavailable", retryable: true });
    expect(classifyError(new ApiHttpError("x", 429, 7))).toMatchObject({ kind: "rate_limited", retryAfterSeconds: 7 });
    expect(classifyError(new ApiHttpError("x", 404))).toMatchObject({ kind: "unknown", retryable: false });
  });

  it("local messages and unknown throwables", () => {
    expect(classifyError("Enter a name.")).toMatchObject({ kind: "input", headline: null, message: "Enter a name.", retryable: false });
    expect(classifyError(new Error("boom")).kind).toBe("unknown");
    expect(classifyError(undefined).message).toBe("Unexpected error talking to the backend.");
  });

  it("carries Retry-After only for the codes where it applies", () => {
    expect(classifyError(envelope(429, "THROTTLED", 12)).retryAfterSeconds).toBe(12);
    expect(classifyError(envelope(503, "UNAVAILABLE", 3)).retryAfterSeconds).toBe(3);
    expect(classifyError(envelope(422, "VALIDATION", 3)).retryAfterSeconds).toBeUndefined();
  });

  it("formats waits", () => {
    expect(formatWait(5)).toBe("5 s");
    expect(formatWait(119)).toBe("119 s");
    expect(formatWait(120)).toBe("2 min");
    expect(formatWait(121)).toBe("3 min");
  });
});

describe("AsyncError", () => {
  it("keeps the legacy sentence and adds a headline and hint", () => {
    render(<AsyncError error={envelope(502, "MODEL_FAILURE")} prefix="Extraction failed." suffix="Your inputs are unchanged." manualHint="Type it by hand." />);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveAttribute("data-kind", "model_unavailable");
    expect(alert).toHaveTextContent("The AI model is unavailable");
    expect(alert).toHaveTextContent("Extraction failed. MODEL_FAILURE message [MODEL_FAILURE, request req-1] Your inputs are unchanged.");
    expect(alert).toHaveTextContent("The deterministic calculations are not affected");
    expect(alert).toHaveTextContent("Type it by hand.");
  });

  it("only mentions the manual alternative for a model outage", () => {
    render(<AsyncError error={envelope(503, "UNAVAILABLE")} manualHint="Type it by hand." />);
    expect(screen.getByRole("alert")).not.toHaveTextContent("Type it by hand.");
  });

  it("offers a deliberate retry only when retrying can help", async () => {
    const user = userEvent.setup();
    const onRetry = vi.fn();
    const { rerender } = render(<AsyncError error={new ApiNetworkError("Cannot reach")} onRetry={onRetry} />);
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(onRetry).toHaveBeenCalledTimes(1);

    for (const error of [envelope(422, "VALIDATION"), envelope(400, "BAD_JSON"), new ModeMismatchError("m"), "local message"]) {
      rerender(<AsyncError error={error} onRetry={onRetry} />);
      expect(screen.queryByRole("button", { name: /Try again/ })).not.toBeInTheDocument();
    }
  });

  it("without onRetry there is no button even for a transient error", () => {
    render(<AsyncError error={new ApiTimeoutError("slow")} />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("honours Retry-After: the button counts down and enables only when the wait is over", () => {
    vi.useFakeTimers();
    render(<AsyncError error={envelope(429, "THROTTLED", 3)} onRetry={() => {}} />);
    expect(screen.getByTestId("retry-after")).toHaveTextContent("wait of 3 s");
    const button = screen.getByRole("button", { name: "Try again in 3 s" });
    expect(button).toHaveAttribute("aria-disabled", "true");
    act(() => void vi.advanceTimersByTime(1100));
    expect(screen.getByRole("button", { name: "Try again in 2 s" })).toHaveAttribute("aria-disabled", "true");
    act(() => void vi.advanceTimersByTime(2000));
    expect(screen.getByRole("button", { name: "Try again" })).toHaveAttribute("aria-disabled", "false");
  });

  it("restarts the wait for a new error", () => {
    vi.useFakeTimers();
    const first = envelope(429, "THROTTLED", 2);
    const { rerender } = render(<AsyncError error={first} onRetry={() => {}} />);
    act(() => void vi.advanceTimersByTime(2500));
    expect(screen.getByRole("button", { name: "Try again" })).toHaveAttribute("aria-disabled", "false");
    rerender(<AsyncError error={envelope(429, "THROTTLED", 5)} onRetry={() => {}} />);
    expect(screen.getByRole("button", { name: "Try again in 5 s" })).toHaveAttribute("aria-disabled", "true");
  });

  it("a 429 without Retry-After retries immediately on the user's say-so", () => {
    render(<AsyncError error={envelope(429, "THROTTLED")} onRetry={() => {}} />);
    expect(screen.queryByTestId("retry-after")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toHaveAttribute("aria-disabled", "false");
  });
});

describe("Pending and EmptyState", () => {
  it("Pending is a polite busy status, not an alert", () => {
    render(<Pending>Working…</Pending>);
    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("aria-busy", "true");
    expect(status).toHaveTextContent("Working…");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("EmptyState says why there is nothing", () => {
    render(<EmptyState testId="empty">Nothing yet because X.</EmptyState>);
    expect(screen.getByTestId("empty")).toHaveTextContent("Nothing yet because X.");
  });
});

describe("useLatestRequest", () => {
  it("refuses a second request while one is running (no double submit)", () => {
    const { result } = renderHook(() => useLatestRequest());
    const first = result.current.start();
    expect(first).not.toBeNull();
    expect(result.current.start()).toBeNull();
    first!.done();
    expect(result.current.start()).not.toBeNull();
  });

  it("invalidate aborts the running request, marks it stale and frees the slot", () => {
    const { result } = renderHook(() => useLatestRequest());
    const first = result.current.start()!;
    expect(first.isCurrent()).toBe(true);
    result.current.invalidate();
    expect(first.signal.aborted).toBe(true);
    expect(first.isCurrent()).toBe(false);
    const second = result.current.start();
    expect(second).not.toBeNull();
    first.done(); // a stale request must not free the slot of the new one
    expect(result.current.start()).toBeNull();
    second!.done();
  });

  it("aborts on unmount", () => {
    const { result, unmount } = renderHook(() => useLatestRequest());
    const request = result.current.start()!;
    unmount();
    expect(request.signal.aborted).toBe(true);
  });
});

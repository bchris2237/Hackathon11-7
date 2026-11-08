import { useEffect, useRef, useState, type ReactNode } from "react";

/**
 * Wraps content that may be wider than the screen (tables). When it really does overflow, the
 * wrapper becomes a labelled, keyboard-focusable group so people without a mouse can scroll it
 * (arrow keys), and a visible hint says so. When everything fits it adds no tab stop and no hint.
 */
export default function ScrollRegion({ label, children }: { label: string; children: ReactNode }) {
  const box = useRef<HTMLDivElement>(null);
  const [scrolls, setScrolls] = useState(false);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const check = () => setScrolls(el.scrollWidth > el.clientWidth + 1);
    check();
    if (typeof ResizeObserver === "undefined") return;
    // Deferred a frame so measuring never triggers a resize loop.
    let frame = 0;
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(check);
    });
    observer.observe(el);
    if (el.firstElementChild) observer.observe(el.firstElementChild);
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, []);

  return (
    <>
      {scrolls && (
        <p aria-hidden="true" className="text-xs text-slate-600" data-testid="scroll-hint">
          Scroll sideways to see all columns →
        </p>
      )}
      <div
        ref={box}
        className="overflow-x-auto"
        {...(scrolls ? { role: "group", tabIndex: 0, "aria-label": `${label} (scrolls sideways)` } : {})}
      >
        {children}
      </div>
    </>
  );
}

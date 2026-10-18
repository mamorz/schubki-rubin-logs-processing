import { ArrowDown } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode } from "react";

const THUMB = 22;

/** Scroll area with a dark track and a draggable gem as the thumb (any icon can be passed as `thumb`). */
export function GemScroll({
  children,
  className = "",
  contentClassName = "",
  thumb = <RubyGem />,
  label = "Scroll conversation",
  followKey,
}: {
  children: ReactNode;
  className?: string;
  contentClassName?: string;
  thumb?: ReactNode;
  label?: string;
  /** When set, new content keeps the view pinned to the bottom only while the user is already there;
   * changing the key (e.g. a new question) jumps back down. */
  followKey?: string | number;
}) {
  const id = useId();
  const viewport = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const track = useRef<HTMLDivElement>(null);
  const grab = useRef<number | null>(null);
  const atBottom = useRef(true);
  const following = followKey !== undefined;
  const [state, setState] = useState({ top: 0, percent: 0, scrollable: false, dragging: false, atBottom: true });

  const measure = useCallback(() => {
    const v = viewport.current;
    const t = track.current;
    if (!v || !t) return;
    const max = v.scrollHeight - v.clientHeight;
    const range = Math.max(0, t.clientHeight - THUMB);
    const ratio = max > 0 ? v.scrollTop / max : 0;
    setState((s) => ({ ...s, top: ratio * range, percent: Math.round(ratio * 100), scrollable: max > 4, atBottom: atBottom.current }));
  }, []);

  const onScroll = useCallback(() => {
    const v = viewport.current;
    if (v) atBottom.current = v.scrollHeight - v.scrollTop - v.clientHeight < 48;
    measure();
  }, [measure]);

  const scrollToBottom = useCallback((smooth = false) => {
    const v = viewport.current;
    if (!v) return;
    atBottom.current = true;
    v.scrollTo({ top: v.scrollHeight, behavior: smooth ? "smooth" : "auto" });
  }, []);

  // Content grew (streaming answer, new tool call): follow only if the reader was at the bottom.
  const onResize = useCallback(() => {
    if (following && atBottom.current) scrollToBottom();
    measure();
  }, [following, measure, scrollToBottom]);

  useEffect(() => {
    if (following) scrollToBottom();
  }, [followKey, following, scrollToBottom]);

  useEffect(() => {
    const v = viewport.current;
    if (!v) return;
    v.addEventListener("scroll", onScroll, { passive: true });
    const observer = new ResizeObserver(onResize);
    observer.observe(v);
    if (content.current) observer.observe(content.current);
    measure();
    return () => {
      v.removeEventListener("scroll", onScroll);
      observer.disconnect();
    };
  }, [measure, onScroll, onResize]);

  const scrollToRatio = (ratio: number, smooth = false) => {
    const v = viewport.current;
    if (!v) return;
    const top = Math.min(1, Math.max(0, ratio)) * (v.scrollHeight - v.clientHeight);
    v.scrollTo({ top, behavior: smooth ? "smooth" : "auto" });
  };

  const ratioAt = (clientY: number, offset: number) => {
    const rect = track.current!.getBoundingClientRect();
    return (clientY - rect.top - offset) / Math.max(1, rect.height - THUMB);
  };

  const onThumbDown = (event: PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.stopPropagation();
    event.currentTarget.setPointerCapture(event.pointerId);
    grab.current = event.clientY - event.currentTarget.getBoundingClientRect().top;
    setState((s) => ({ ...s, dragging: true }));
  };

  const onThumbMove = (event: PointerEvent<HTMLDivElement>) => {
    if (grab.current !== null) scrollToRatio(ratioAt(event.clientY, grab.current));
  };

  const onThumbUp = () => {
    grab.current = null;
    setState((s) => ({ ...s, dragging: false }));
  };

  const onTrackDown = (event: PointerEvent<HTMLDivElement>) => {
    if (event.target === event.currentTarget) scrollToRatio(ratioAt(event.clientY, THUMB / 2), true);
  };

  const onKey = (event: KeyboardEvent<HTMLDivElement>) => {
    const v = viewport.current;
    if (!v) return;
    const steps: Record<string, number> = { ArrowDown: 80, ArrowUp: -80, PageDown: v.clientHeight * 0.9, PageUp: -v.clientHeight * 0.9 };
    if (event.key in steps) v.scrollBy({ top: steps[event.key] });
    else if (event.key === "Home") v.scrollTo({ top: 0 });
    else if (event.key === "End") v.scrollTo({ top: v.scrollHeight });
    else return;
    event.preventDefault();
  };

  return (
    <div className={`relative ${className}`}>
      <div
        id={id}
        ref={viewport}
        className="h-full overflow-y-auto pr-8 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
      >
        <div ref={content} className={contentClassName}>{children}</div>
      </div>

      {following && state.scrollable && !state.atBottom && (
        <button
          type="button"
          onClick={() => scrollToBottom(true)}
          className="absolute bottom-3 left-1/2 z-10 flex -translate-x-1/2 items-center gap-1.5 rounded-full bg-slate-950 px-3 py-1.5 text-xs font-medium text-ruby-100 shadow-lg ring-1 ring-ruby-500/40 transition hover:ring-ruby-400"
        >
          <ArrowDown className="size-3.5 text-ruby-400" /> Latest
        </button>
      )}

      <div
        className={`absolute top-3 right-2 bottom-3 w-[22px] transition-opacity duration-300 ${state.scrollable ? "opacity-100" : "pointer-events-none opacity-0"}`}
      >
        <div
          ref={track}
          onPointerDown={onTrackDown}
          className="absolute inset-y-0 left-1/2 w-2.5 -translate-x-1/2 cursor-pointer rounded-full bg-slate-950 shadow-[inset_0_1px_3px_rgba(0,0,0,0.6)] ring-1 ring-white/5 dark:bg-black dark:ring-white/10"
        >
          {/* Ruby trail from the top to the gem marks how far you have read. */}
          <div
            className="pointer-events-none absolute top-1 left-1/2 w-0.5 -translate-x-1/2 rounded-full bg-gradient-to-b from-ruby-900 via-ruby-700 to-ruby-500 opacity-80"
            style={{ height: Math.max(0, state.top + THUMB / 2 - 4) }}
          />
        </div>
        <div
          role="scrollbar"
          aria-controls={id}
          aria-orientation="vertical"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={state.percent}
          aria-label={label}
          tabIndex={0}
          onPointerDown={onThumbDown}
          onPointerMove={onThumbMove}
          onPointerUp={onThumbUp}
          onPointerCancel={onThumbUp}
          onKeyDown={onKey}
          className={`absolute left-0 grid size-[22px] touch-none place-items-center rounded-full outline-none select-none focus-visible:ring-2 focus-visible:ring-ruby-400 ${
            state.dragging ? "scale-125 cursor-grabbing" : "cursor-grab transition-transform hover:scale-110"
          }`}
          style={{ top: state.top }}
        >
          {thumb}
        </div>
      </div>
    </div>
  );
}

/** Faceted ruby, drawn for a dark background. */
export function RubyGem({ size = 22 }: { size?: number }) {
  const gid = useId();
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true" className="drop-shadow-[0_0_6px_rgba(244,63,94,0.75)]">
      <defs>
        <linearGradient id={`${gid}-crown`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#fda4af" />
          <stop offset="1" stopColor="#e11d48" />
        </linearGradient>
        <linearGradient id={`${gid}-pavilion`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#e11d48" />
          <stop offset="1" stopColor="#4c0519" />
        </linearGradient>
      </defs>
      <path d="M2 9 L12 22 L22 9 Z" fill={`url(#${gid}-pavilion)`} />
      <path d="M6 3 H18 L22 9 H2 Z" fill={`url(#${gid}-crown)`} />
      <path d="M9 9 L12 22 L15 9 Z" fill="#be123c" />
      <path d="M6 3 L9 9 L12 3 L15 9 L18 3" fill="none" stroke="#fff1f2" strokeOpacity="0.55" strokeWidth="0.6" strokeLinejoin="round" />
      <path d="M2 9 H22 M9 9 L12 22 L15 9" fill="none" stroke="#881337" strokeOpacity="0.8" strokeWidth="0.6" strokeLinejoin="round" />
      <path d="M6 3 H18 L22 9 L12 22 L2 9 Z" fill="none" stroke="#4c0519" strokeWidth="0.8" strokeLinejoin="round" />
      <path d="M8 4.2 L10 4.2 L8.8 6.8 Z" fill="#fff" fillOpacity="0.7" />
    </svg>
  );
}

import { useEffect, useRef, useState } from "react";

const MIN_CPS = 40;
const MAX_CPS = 240;
const DRAIN_SECONDS = 0.25;
const SNAP_LAG = 800;
const MAX_FRAME_DT = 0.1;

/**
 * Smooths a streaming string into a steady character-by-character reveal,
 * decoupling render cadence from token arrival. The reveal accelerates when
 * the backlog grows and decelerates when it drains.
 */
export function useSmoothStream(target: string, isStreaming: boolean): string {
  const [displayedLen, setDisplayedLen] = useState<number>(() =>
    isStreaming ? 0 : target.length,
  );

  const targetRef = useRef(target);
  targetRef.current = target;

  useEffect(() => {
    setDisplayedLen((cur) => (cur > target.length ? target.length : cur));
  }, [target]);

  const isActive = isStreaming || displayedLen < target.length;

  useEffect(() => {
    if (!isActive) return;

    let raf = 0;
    let carry = 0;
    let lastTime = 0;

    const tick = (now: number) => {
      const dt = lastTime ? Math.min(MAX_FRAME_DT, (now - lastTime) / 1000) : 0;
      lastTime = now;

      setDisplayedLen((current) => {
        const targetLen = targetRef.current.length;
        const lag = targetLen - current;
        if (lag <= 0) return current;
        if (lag >= SNAP_LAG) {
          carry = 0;
          return targetLen;
        }
        const cps = Math.min(
          MAX_CPS,
          Math.max(MIN_CPS, lag / DRAIN_SECONDS),
        );
        carry += cps * dt;
        const step = Math.floor(carry);
        if (step <= 0) return current;
        carry -= step;
        return Math.min(targetLen, current + step);
      });

      raf = requestAnimationFrame(tick);
    };

    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [isActive]);

  return displayedLen >= target.length
    ? target
    : target.slice(0, displayedLen);
}

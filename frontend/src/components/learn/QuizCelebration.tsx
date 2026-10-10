import { useEffect, useRef } from "react";

const colors = [
  "#f5c451",
  "#ff5da2",
  "#6d28d9",
  "#4fd1e8",
  "#ffffff",
  "#ff9f43",
  "#7bed9f",
];
const between = (min: number, max: number) => min + Math.random() * (max - min);

// Burst motion adapted from https://codepen.io/darshit_tank/pen/emgVOWg
export function QuizCelebration({
  origin,
  onDone,
}: {
  origin: { x: number; y: number };
  onDone: () => void;
}) {
  const layer = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const container = layer.current;
    if (!container) return;
    const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
    if (motion.matches) {
      onDone();
      return;
    }
    const particles = Array.from({ length: 100 }, (_, i) => {
      const left = i < 70;
      const element = document.createElement("span");
      const size = between(6, 14);
      Object.assign(element.style, {
        position: "absolute",
        top: "0",
        left: "0",
        width: `${size}px`,
        height: `${size}px`,
        background: colors[Math.floor(Math.random() * colors.length)],
        borderRadius: Math.random() > 0.5 ? "50%" : "2px",
        willChange: "transform, opacity",
      });
      container.appendChild(element);
      const angle = (between(55, 85) * Math.PI) / 180;
      const speed = between(11, 19);
      const x = origin.x;
      const y = origin.y;
      element.style.transform = `translate(${x}px, ${y}px)`;
      return {
        element,
        x,
        y,
        vx: Math.cos(angle) * speed * (left ? 1 : -1) * between(0.7, 1.3),
        vy: -Math.sin(angle) * speed,
        rotation: between(0, 360),
        rotSpeed: between(-14, 14),
        gravity: between(0.35, 0.55),
        life: 0,
        maxLife: between(90, 140),
      };
    });
    let frame = 0;
    let previous = performance.now();
    let elapsed = 0;
    function tick(now: number) {
      // Normalize the reference's 60fps physics for high-refresh-rate displays.
      const step = Math.min((now - previous) / (1000 / 60), 2);
      elapsed += now - previous;
      previous = now;
      let alive = false;
      for (const p of particles) {
        if (p.life >= p.maxLife) {
          p.element.style.opacity = "0";
          continue;
        }
        alive = true;
        p.vy += p.gravity * step;
        p.x += p.vx * step;
        p.y += p.vy * step;
        p.rotation += p.rotSpeed * step;
        p.life += step;
        const fadeStart = p.maxLife * 0.6;
        p.element.style.opacity = String(
          Math.max(
            0,
            1 - Math.max(0, p.life - fadeStart) / (p.maxLife - fadeStart),
          ),
        );
        p.element.style.transform = `translate(${p.x}px, ${p.y}px) rotate(${p.rotation}deg)`;
      }
      if (alive && elapsed < 3000) frame = requestAnimationFrame(tick);
      else onDone();
    }
    frame = requestAnimationFrame(tick);
    const timer = window.setTimeout(onDone, 3200);
    const stopForMotion = () => {
      if (motion.matches) onDone();
    };
    motion.addEventListener("change", stopForMotion);
    return () => {
      cancelAnimationFrame(frame);
      window.clearTimeout(timer);
      motion.removeEventListener("change", stopForMotion);
      container.replaceChildren();
    };
  }, [origin, onDone]);
  return (
    <div
      ref={layer}
      aria-hidden="true"
      className="fixed inset-0 z-60 pointer-events-none overflow-hidden motion-reduce:hidden"
    />
  );
}

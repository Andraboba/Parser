import { type PointerEvent, useMemo, useState } from "react";

import type { SpeedSample } from "./api";
import { rate } from "./format";

const WIDTH = 720;
const HEIGHT = 180;
const PADDING = { top: 10, right: 12, bottom: 22, left: 44 };

interface Point {
  time: number;
  speed: number;
}

/** Скорость между соседними снимками прогресса. Один ряд: заголовок называет его, легенда не нужна. */
function toPoints(samples: SpeedSample[]): Point[] {
  const points: Point[] = [];
  for (let i = 1; i < samples.length; i += 1) {
    const elapsed = (Date.parse(samples[i].at) - Date.parse(samples[i - 1].at)) / 1000;
    if (elapsed > 0) {
      points.push({
        time: Date.parse(samples[i].at),
        speed: Math.max(0, samples[i].processed_ids - samples[i - 1].processed_ids) / elapsed,
      });
    }
  }
  return points;
}

function niceMax(value: number): number {
  if (value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const step = [1, 2, 5, 10].find((m) => m * magnitude >= value) ?? 10;
  return step * magnitude;
}

function clock(time: number): string {
  return new Date(time).toLocaleTimeString("ru-RU");
}

export function SpeedChart({ samples }: { samples: SpeedSample[] }) {
  const [hover, setHover] = useState<number | null>(null);
  const points = useMemo(() => toPoints(samples), [samples]);

  if (points.length < 2) {
    return <p className="muted">График появится, когда задание поработает несколько секунд.</p>;
  }

  const first = points[0].time;
  const last = points[points.length - 1].time;
  const top = niceMax(Math.max(...points.map((p) => p.speed)));
  const innerWidth = WIDTH - PADDING.left - PADDING.right;
  const innerHeight = HEIGHT - PADDING.top - PADDING.bottom;
  const x = (time: number) => PADDING.left + ((time - first) / Math.max(1, last - first)) * innerWidth;
  const y = (speed: number) => PADDING.top + innerHeight - (speed / top) * innerHeight;
  const path = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${x(p.time).toFixed(1)},${y(p.speed).toFixed(1)}`)
    .join(" ");

  const onMove = (event: PointerEvent<SVGRectElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    const time = first + ((event.clientX - box.left) / box.width) * (last - first);
    let nearest = 0;
    points.forEach((point, index) => {
      if (Math.abs(point.time - time) < Math.abs(points[nearest].time - time)) nearest = index;
    });
    setHover(nearest);
  };

  const active = hover === null ? null : points[Math.min(hover, points.length - 1)];
  return (
    <div className="chart">
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        role="img"
        aria-label={`Скорость обработки, сейчас ${rate(points[points.length - 1].speed)} идентификаторов в секунду`}
      >
        {[0, 0.5, 1].map((part) => (
          <g key={part}>
            <line
              className="chart-grid"
              x1={PADDING.left}
              x2={WIDTH - PADDING.right}
              y1={y(top * part)}
              y2={y(top * part)}
            />
            <text className="chart-axis" x={PADDING.left - 6} y={y(top * part) + 4} textAnchor="end">
              {rate(top * part)}
            </text>
          </g>
        ))}
        <text className="chart-axis" x={PADDING.left} y={HEIGHT - 6}>
          {clock(first)}
        </text>
        <text className="chart-axis" x={WIDTH - PADDING.right} y={HEIGHT - 6} textAnchor="end">
          {clock(last)}
        </text>
        <path className="chart-line" d={path} />
        {active && (
          <>
            <line
              className="chart-cursor"
              x1={x(active.time)}
              x2={x(active.time)}
              y1={PADDING.top}
              y2={PADDING.top + innerHeight}
            />
            <circle className="chart-dot" cx={x(active.time)} cy={y(active.speed)} r={5} />
          </>
        )}
        <rect
          x={PADDING.left}
          y={PADDING.top}
          width={innerWidth}
          height={innerHeight}
          fill="transparent"
          onPointerMove={onMove}
          onPointerLeave={() => setHover(null)}
        />
      </svg>
      {active && (
        <div
          className="chart-tooltip"
          style={
            x(active.time) > WIDTH / 2
              ? { right: `${((WIDTH - x(active.time)) / WIDTH) * 100}%` }
              : { left: `${(x(active.time) / WIDTH) * 100}%` }
          }
        >
          {clock(active.time)}: {rate(active.speed)} ид./с
        </div>
      )}
    </div>
  );
}

/**
 * Composer-native adaptation of the effort slider visual and interaction from
 * dsh-codex-effort-slider at af723caf3387e64ae28aa69c4fd235b1b662e3ae.
 * Upstream source: https://github.com/Microqian2th/dsh-codex-effort-slider
 * The DSH-specific DOM bridge is not used here; values come from this app's
 * reasoning-effort catalog and update its existing Composer state.
 *
 * MIT License
 * Copyright (c) 2026 dsh-codex-effort-slider contributors
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in all
 * copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */

import { type ChangeEvent, type CSSProperties, type KeyboardEvent } from 'react';
import type { CatalogEntry } from '../lib/api';

interface Props {
  options: CatalogEntry[];
  value: string | null;
  defaultValue?: string | null;
  disabled?: boolean;
  onChange: (value: string | null) => void;
}

type SliderStyle = CSSProperties & {
  '--effort-fill': string;
  '--effort-energy': string;
  '--effort-color': string;
};

const PARTICLE_COUNT = 22;

/* 颜色只来自 token（#1364）：三个停靠点与 index.css 的 `--effort-track-start` /
 * `--effort-gradient-3` / `--effort-gradient-end` 同源。混色交给 CSS
 * `color-mix(in srgb, ...)`：与原先逐通道线性插值等价（sRGB 不透明混色，误差 ≤1/255），
 * 且不需要在 TSX 里保留任何颜色字面量。 */
const TRACK_START = 'var(--effort-track-start)';
const TRACK_VIOLET = 'var(--effort-gradient-3)';
const TRACK_DEEP = 'var(--effort-gradient-end)';

function mixColor(from: string, to: string, amount: number): string {
  return `color-mix(in srgb, ${to} ${(amount * 100).toFixed(3)}%, ${from})`;
}

function colorAt(progress: number): string {
  if (progress <= 1 / 3) return TRACK_START;
  if (progress <= 2 / 3) return mixColor(TRACK_START, TRACK_VIOLET, (progress - 1 / 3) * 3);
  return mixColor(TRACK_VIOLET, TRACK_DEEP, (progress - 2 / 3) * 3);
}

function toneAt(optionIndex: number, optionCount: number): 'default' | 'blue' | 'violet' | 'deep' {
  if (optionIndex < 0) return 'default';
  if (optionIndex === 0) return 'blue';
  if (optionIndex === optionCount - 1) return 'deep';
  return 'violet';
}

function offsetAt(progress: number): string {
  const percent = (progress * 100).toFixed(3);
  const correction = (14 * (1 - 2 * progress)).toFixed(3);
  return 'calc(' + percent + '% + ' + correction + 'px)';
}

export function ReasoningEffortSlider({
  options,
  value,
  defaultValue = null,
  disabled = false,
  onChange,
}: Props) {
  if (options.length === 0) return null;

  const labels = ['默认', ...options.map((option) => option.display_name)];
  const defaultOption = options.find((option) => option.id === defaultValue);
  const selectedOptionIndex = value === null ? -1 : options.findIndex((option) => option.id === value);
  const selectedIndex = selectedOptionIndex < 0 ? 0 : selectedOptionIndex + 1;
  const lastIndex = labels.length - 1;
  const progress = lastIndex > 0 ? selectedIndex / lastIndex : 0;
  const effortProgress = selectedOptionIndex < 0
    ? 0
    : options.length > 1
      ? selectedOptionIndex / (options.length - 1)
      : 0;
  const energy = effortProgress * effortProgress * (3 - 2 * effortProgress);
  const currentLabel = labels[selectedIndex] ?? '默认';
  const currentValueLabel =
    selectedIndex === 0 && defaultOption
      ? `${currentLabel} · ${defaultOption.display_name}`
      : currentLabel;
  const tone = toneAt(selectedOptionIndex, options.length);
  const selectedOption = selectedOptionIndex >= 0 ? options[selectedOptionIndex] : null;
  const valueText =
    selectedIndex === 0
      ? defaultOption
        ? `Default, uses ${defaultOption.display_name}; request omits this field`
        : '默认（未选），使用后端默认值'
      : selectedOption?.description
        ? currentLabel + '，' + selectedOption.description
        : currentLabel;
  const visualStyle = {
    '--effort-fill': offsetAt(progress),
    '--effort-energy': energy.toFixed(3),
    '--effort-color': colorAt(effortProgress),
  } as SliderStyle;

  const handleChange = (event: ChangeEvent<HTMLInputElement>) => {
    const nextIndex = Number(event.currentTarget.value);
    onChange(nextIndex === 0 ? null : options[nextIndex - 1]?.id ?? null);
  };
  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (
      event.key === 'ArrowLeft' ||
      event.key === 'ArrowRight' ||
      event.key === 'ArrowUp' ||
      event.key === 'ArrowDown' ||
      event.key === 'Home' ||
      event.key === 'End'
    ) {
      event.stopPropagation();
    }
  };

  return (
    <div className="reasoning-effort-slider" data-disabled={disabled ? "true" : undefined}>
      <div className="reasoning-effort-slider__meta">
        <span>当前档位</span>
        <span className="reasoning-effort-slider__current" data-tone={tone} title={currentValueLabel}>
          {currentValueLabel}
        </span>
      </div>
      <div className="reasoning-effort-slider__track" data-energy={energy > 0 ? 'on' : 'off'} style={visualStyle}>
        <div className="reasoning-effort-slider__fill" aria-hidden="true" />
        <div className="reasoning-effort-slider__energy" aria-hidden="true">
          <span className="reasoning-effort-slider__sweep" />
        </div>
        <div className="reasoning-effort-slider__stars" aria-hidden="true">
          {Array.from({ length: Math.round(PARTICLE_COUNT * energy) }, (_, index) => {
            const vertical = (((index + 1) * 0.6180339887498949) % 1) * 100;
            const phase = ((index + 1) * 0.7548776662466927) % 1;
            const duration = (6.4 - progress * 3.2) * (0.9 + (index % 5) * 0.05);
            const brightness = 0.55 + (((index + 1) * 0.5698402909980532) % 1) * 0.45;
            const particleStyle = {
              top: vertical.toFixed(2) + '%',
              animationDuration: duration.toFixed(2) + 's',
              animationDelay: '-' + (phase * duration).toFixed(2) + 's',
              '--effort-star-x': (phase * 100).toFixed(2) + '%',
              '--effort-star-brightness': brightness.toFixed(2),
            } as CSSProperties;

            return (
              <span className="reasoning-effort-slider__star" key={index} style={particleStyle}>
                <span className="reasoning-effort-slider__star-dot" />
              </span>
            );
          })}
        </div>
        {labels.map((label, index) => (
          <span
            className="reasoning-effort-slider__tick"
            data-passed={index <= selectedIndex ? 'true' : undefined}
            key={label + ':' + index}
            style={{ left: offsetAt(lastIndex > 0 ? index / lastIndex : 0) }}
          />
        ))}
        <input
          aria-label="推理档位"
          aria-valuetext={valueText}
          className="reasoning-effort-slider__input"
          disabled={disabled}
          max={lastIndex}
          min={0}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          step={1}
          title="拖动档位，或使用方向键调整。"
          type="range"
          value={selectedIndex}
        />
      </div>
      <div
        aria-hidden="true"
        className="reasoning-effort-slider__labels"
        style={{ gridTemplateColumns: 'repeat(' + labels.length + ', minmax(0, 1fr))' }}
      >
        {labels.map((label, index) => (
          <span
            data-active={index === selectedIndex ? 'true' : undefined}
            key={label + ':' + index}
            style={{ textAlign: index === 0 ? 'left' : index === lastIndex ? 'right' : 'center' }}
            title={label}
          >
            {label}
          </span>
        ))}
      </div>
    </div>
  );
}

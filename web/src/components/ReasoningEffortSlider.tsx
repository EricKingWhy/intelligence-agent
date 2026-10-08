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
const BLUE = [77, 147, 248] as const;
const VIOLET = [147, 51, 234] as const;
const DEEP_VIOLET = [76, 29, 149] as const;

function mixColor(from: readonly number[], to: readonly number[], amount: number): string {
  const channels = from.map((value, index) =>
    Math.round(value + ((to[index] ?? value) - value) * amount)
      .toString(16)
      .padStart(2, '0'),
  );
  return '#' + channels.join('');
}

function colorAt(progress: number): string {
  if (progress <= 1 / 3) return '#4d93f8';
  if (progress <= 2 / 3) return mixColor(BLUE, VIOLET, (progress - 1 / 3) * 3);
  return mixColor(VIOLET, DEEP_VIOLET, (progress - 2 / 3) * 3);
}

function toneAt(index: number, progress: number): 'default' | 'blue' | 'violet' | 'deep' {
  if (index === 0) return 'default';
  if (progress <= 1 / 3) return 'blue';
  if (progress <= 2 / 3) return 'violet';
  return 'deep';
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
  const energyPosition = Math.min(1, Math.max(0, (progress - 1 / 3) / (2 / 3)));
  const energy = energyPosition * energyPosition * (3 - 2 * energyPosition);
  const currentLabel = labels[selectedIndex] ?? '默认';
  const currentValueLabel =
    selectedIndex === 0 && defaultOption
      ? `${currentLabel} · ${defaultOption.display_name}`
      : currentLabel;
  const tone = toneAt(selectedIndex, progress);
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
    '--effort-color': colorAt(progress),
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
          aria-label="Reasoning Effort slider"
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

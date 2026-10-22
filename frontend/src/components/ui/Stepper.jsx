import React from 'react';
import { motion } from 'framer-motion';
import { Minus, Plus } from 'lucide-react';

/**
 * Stepper - Giant "-" and "+" buttons surrounding a high-contrast numeric readout.
 * Allows field operators to adjust hours, flow rates, or inventory quantities without a keyboard.
 */
export function Stepper({
  label,
  value = 0,
  onChange,
  step = 1,
  min = 0,
  max = 999,
  unit = '',
  disabled = false,
  className = '',
}) {
  const canDecrement = !disabled && value - step >= min;
  const canIncrement = !disabled && value + step <= max;

  const handleDecrement = () => {
    if (!canDecrement) return;
    const next = Number((value - step).toFixed(2));
    onChange?.(Math.max(min, next));
  };

  const handleIncrement = () => {
    if (!canIncrement) return;
    const next = Number((value + step).toFixed(2));
    onChange?.(Math.min(max, next));
  };

  return (
    <div
      className={`bg-dark-surface border-2 border-dark-border rounded-2xl p-4 select-none ${className}`}
    >
      {label && (
        <div className="text-sm font-black uppercase tracking-widest text-neutral-300 mb-3">
          {label}
        </div>
      )}

      <div className="flex items-center justify-between gap-4">
        {/* Giant Decrement Button */}
        <motion.button
          type="button"
          aria-label={`Decrease ${label || 'value'}`}
          onClick={handleDecrement}
          disabled={!canDecrement}
          whileTap={canDecrement ? { scale: 0.93 } : undefined}
          className="
            min-w-touch-lg min-h-touch-lg w-20 h-20 rounded-xl
            bg-black text-white border-2 border-white
            flex items-center justify-center
            touch-manipulation transition-colors
            hover:bg-neutral-900 active:bg-safety-orange active:text-black active:border-black
            focus:outline-none focus:ring-4 focus:ring-primary
            disabled:opacity-30 disabled:pointer-events-none
          "
        >
          <Minus className="w-10 h-10 stroke-[3]" aria-hidden="true" />
        </motion.button>

        {/* High-Contrast Value Display */}
        <div
          className="flex-1 text-center px-2"
          aria-live="polite"
          aria-atomic="true"
        >
          <span className="text-4xl sm:text-5xl font-black text-white tabular-nums tracking-tight">
            {value}
          </span>
          {unit && (
            <span className="block text-sm font-extrabold uppercase tracking-wider text-primary mt-1">
              {unit}
            </span>
          )}
        </div>

        {/* Giant Increment Button */}
        <motion.button
          type="button"
          aria-label={`Increase ${label || 'value'}`}
          onClick={handleIncrement}
          disabled={!canIncrement}
          whileTap={canIncrement ? { scale: 0.93 } : undefined}
          className="
            min-w-touch-lg min-h-touch-lg w-20 h-20 rounded-xl
            bg-primary text-black border-2 border-black
            flex items-center justify-center
            touch-manipulation transition-colors
            hover:bg-primary-active active:bg-safety-orange
            focus:outline-none focus:ring-4 focus:ring-white
            disabled:opacity-30 disabled:pointer-events-none
          "
        >
          <Plus className="w-10 h-10 stroke-[3]" aria-hidden="true" />
        </motion.button>
      </div>
    </div>
  );
}

export default Stepper;

import React from 'react';
import { motion } from 'framer-motion';
import { Check } from 'lucide-react';

/**
 * QuickChip - Large toggleable button for selecting shift categories, channels, or equipment tags.
 * Enforces >= 60px touch height and unambiguous active/inactive outdoor states.
 */
export function QuickChip({
  label,
  selected = false,
  onClick,
  icon: Icon,
  disabled = false,
  className = '',
}) {
  return (
    <motion.button
      type="button"
      role="checkbox"
      aria-checked={selected}
      disabled={disabled}
      onClick={onClick}
      whileTap={disabled ? undefined : { scale: 0.96 }}
      className={`
        min-h-touch min-w-touch px-5 py-3 rounded-xl
        inline-flex items-center justify-center gap-3
        font-extrabold text-lg uppercase tracking-wide
        select-none touch-manipulation transition-colors
        focus:outline-none focus:ring-4 focus:ring-primary
        disabled:opacity-40 disabled:pointer-events-none
        ${
          selected
            ? 'bg-primary text-black border-2 border-white shadow-hard-sm'
            : 'bg-dark-surface text-white border-2 border-dark-border hover:border-white'
        }
        ${className}
      `}
    >
      {selected ? (
        <Check className="w-6 h-6 stroke-[3] shrink-0 text-black" aria-hidden="true" />
      ) : (
        Icon && <Icon className="w-6 h-6 stroke-[2.5] shrink-0 text-primary" aria-hidden="true" />
      )}
      <span>{label}</span>
    </motion.button>
  );
}

export default QuickChip;

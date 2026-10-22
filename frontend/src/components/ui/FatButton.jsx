import React from 'react';
import { motion } from 'framer-motion';

const VARIANTS = {
  primary:
    'bg-primary text-black border-2 border-black hover:bg-primary-active shadow-hard',
  orange:
    'bg-safety-orange text-black border-2 border-black hover:brightness-110 shadow-hard',
  secondary:
    'bg-dark-surface text-white border-2 border-white hover:bg-neutral-700 shadow-hard',
  danger:
    'bg-safety-red text-white border-2 border-white hover:bg-red-600 shadow-hard',
  outline:
    'bg-black text-primary border-2 border-primary hover:bg-dark-surface shadow-hard',
};

/**
 * FatButton - Massive, high-contrast button designed for gloved/outdoor field use.
 * Enforces >= 60x60px touch target, bold typography, and tactile press feedback.
 */
export function FatButton({
  children,
  onClick,
  type = 'button',
  variant = 'primary',
  icon: Icon,
  disabled = false,
  fullWidth = true,
  className = '',
  ...props
}) {
  const variantClasses = VARIANTS[variant] || VARIANTS.primary;

  return (
    <motion.button
      type={type}
      onClick={onClick}
      disabled={disabled}
      whileTap={disabled ? undefined : { scale: 0.97, y: 2 }}
      className={`
        min-h-touch min-w-touch px-6 py-4 rounded-xl
        inline-flex items-center justify-center gap-3
        font-extrabold text-xl uppercase tracking-wider
        select-none touch-manipulation transition-colors
        focus:outline-none focus:ring-4 focus:ring-primary focus:ring-offset-2 focus:ring-offset-dark
        disabled:opacity-40 disabled:pointer-events-none
        ${fullWidth ? 'w-full' : ''}
        ${variantClasses}
        ${className}
      `}
      {...props}
    >
      {Icon && <Icon className="w-7 h-7 stroke-[2.75] shrink-0" aria-hidden="true" />}
      <span>{children}</span>
    </motion.button>
  );
}

export default FatButton;

import React from 'react';
import { Truck, HardHat, Warehouse } from 'lucide-react';
import { Stepper } from '../../components/ui/Stepper';
import { QuickChip } from '../../components/ui/QuickChip';

const TIME_CATEGORIES = [
  { id: 'Travel', label: 'Travel', icon: Truck },
  { id: 'On-Site', label: 'On-Site', icon: HardHat },
  { id: 'Shed/Prep', label: 'Shed/Prep', icon: Warehouse },
];

/**
 * Step 2: Log Time
 * Massive Stepper defaulting to 8.0 hours in 0.25 increments,
 * paired with three QuickChip category toggles: Travel, On-Site, Shed/Prep.
 */
export function Step2Time({ hours = 8.0, onChangeHours, category, onChangeCategory }) {
  return (
    <div className="space-y-6">
      <div>
        <span className="text-xs font-black uppercase tracking-widest text-primary">
          Step 2 of 3 • Shift Duration
        </span>
        <h2 className="text-2xl sm:text-3xl font-black uppercase tracking-tight text-white mt-1">
          Log Hours & Work Type
        </h2>
      </div>

      {/* Massive Time Stepper (0.25 hr increments) */}
      <Stepper
        label=""
        value={Number(hours).toFixed(2)}
        onChange={(nextVal) => onChangeHours(Number(nextVal))}
        step={0.25}
        min={0.25}
        max={24}
        unit="Hours (15m steps)"
      />

      {/* Work Category QuickChips */}
      <div className="space-y-3">
        <div className="text-sm font-black uppercase tracking-widest text-neutral-300">
          Select Time Category
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          {TIME_CATEGORIES.map((item) => (
            <QuickChip
              key={item.id}
              label={item.label}
              icon={item.icon}
              selected={category === item.id}
              onClick={() => onChangeCategory(item.id)}
              className="w-full"
            />
          ))}
        </div>
      </div>
    </div>
  );
}

export default Step2Time;

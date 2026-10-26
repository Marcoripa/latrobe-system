import React from 'react';
import { Truck, HardHat, Warehouse } from 'lucide-react';
import { Stepper } from '../../components/ui/Stepper';

export const TIME_CATEGORIES = [
  { id: 'Travel', label: 'Travel', icon: Truck },
  { id: 'On-Site', label: 'On-Site', icon: HardHat },
  { id: 'Shed/Prep', label: 'Shed / Prep', icon: Warehouse },
];

export const MAX_HOURS_PER_CATEGORY = 24;

/**
 * Step 2: Log Time
 * Time is broken down into three separate steppers (0.25 hr increments):
 * Travel, On-Site and Shed/Prep. At least one category must have hours > 0.
 */
export function Step2Time({ hoursByCategory, onChangeHoursByCategory }) {
  const totalHours = TIME_CATEGORIES.reduce(
    (sum, cat) => sum + (Number(hoursByCategory?.[cat.id]) || 0),
    0
  );

  const handleChange = (categoryId, nextVal) => {
    onChangeHoursByCategory({
      ...hoursByCategory,
      [categoryId]: Number(nextVal),
    });
  };

  return (
    <div className="space-y-6">
      <div>
        <span className="text-xs font-black uppercase tracking-widest text-primary">
          Step 2 of 5 • Shift Duration
        </span>
        <h2 className="text-2xl sm:text-3xl font-black uppercase tracking-tight text-white mt-1">
          Log Hours by Work Type
        </h2>
      </div>

      <div className="space-y-4">
        {TIME_CATEGORIES.map(({ id, label, icon: Icon }) => (
          <div key={id} className="space-y-2">
            <div className="flex items-center gap-2 text-primary">
              <Icon className="w-6 h-6 stroke-[2.75]" aria-hidden="true" />
              <span className="text-base font-black uppercase tracking-widest text-white">
                {label}
              </span>
            </div>
            <Stepper
              label={`${label} Hours`}
              value={Number(hoursByCategory?.[id] || 0)}
              onChange={(nextVal) => handleChange(id, nextVal)}
              step={0.25}
              min={0}
              max={MAX_HOURS_PER_CATEGORY}
              unit="Hours (15m steps)"
            />
          </div>
        ))}
      </div>

      <div className="flex items-center justify-between p-4 rounded-2xl bg-black border-4 border-primary">
        <span className="text-sm font-black uppercase tracking-widest text-neutral-300">
          Total Shift Hours
        </span>
        <span className="text-3xl font-black text-white tabular-nums">
          {totalHours.toFixed(2)}
        </span>
      </div>

      {totalHours <= 0 && (
        <p className="text-sm font-extrabold uppercase tracking-wide text-safety-orange">
          Add hours to at least one work type to continue.
        </p>
      )}
    </div>
  );
}

export default Step2Time;

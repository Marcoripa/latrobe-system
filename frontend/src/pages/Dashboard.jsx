import React, { useState } from 'react';
import { Droplets, Wrench, Truck, ShieldAlert, CheckCircle2 } from 'lucide-react';
import { FatButton } from '../components/ui/FatButton';
import { Stepper } from '../components/ui/Stepper';
import { QuickChip } from '../components/ui/QuickChip';

const CATEGORIES = [
  { id: 'irrigation', label: 'Channel Flow', icon: Droplets },
  { id: 'maintenance', label: 'Gate Repair', icon: Wrench },
  { id: 'fleet', label: 'Vehicle Check', icon: Truck },
  { id: 'hazard', label: 'Hazard Report', icon: ShieldAlert },
];

export function Dashboard() {
  const [hours, setHours] = useState(8);
  const [selectedCategories, setSelectedCategories] = useState(['irrigation']);

  const toggleCategory = (id) => {
    setSelectedCategories((prev) =>
      prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id]
    );
  };

  return (
    <div className="space-y-6">
      <div>
        <p className="text-xs font-black uppercase tracking-widest text-primary">
          Shift Entry
        </p>
        <h1 className="text-3xl font-black uppercase tracking-tight">
          Field Shift Dashboard
        </h1>
      </div>

      <section className="space-y-3">
        <h2 className="text-sm font-black uppercase tracking-widest text-neutral-300">
          Select Task Categories
        </h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          {CATEGORIES.map((cat) => (
            <QuickChip
              key={cat.id}
              label={cat.label}
              icon={cat.icon}
              selected={selectedCategories.includes(cat.id)}
              onClick={() => toggleCategory(cat.id)}
            />
          ))}
        </div>
      </section>

      <Stepper
        label="Shift Duration"
        value={hours}
        onChange={setHours}
        step={0.5}
        min={0.5}
        max={24}
        unit="Hours"
      />

      <div className="pt-2">
        <FatButton icon={CheckCircle2} variant="primary">
          Save Shift Offline
        </FatButton>
      </div>
    </div>
  );
}

export default Dashboard;

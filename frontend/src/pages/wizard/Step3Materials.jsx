import React, { useEffect, useState } from 'react';
import {
  PackagePlus,
  Trash2,
  Search,
  X,
  Plus,
  ChevronDown,
  ChevronUp,
} from 'lucide-react';
import { QuickChip } from '../../components/ui/QuickChip';
import { Stepper } from '../../components/ui/Stepper';
import { FatButton } from '../../components/ui/FatButton';
import {
  createMaterialHybrid,
  filterMaterialsLocally,
  getCachedMaterials,
  searchMaterialsHybrid,
} from '../../lib/offlineQueue';

const UNIT_OPTIONS = ['Units', 'Meters', 'Rolls', 'Lengths'];
const QUICK_PICK_LIMIT = 6;

/**
 * Step 3: Log Materials
 * - Search Material input: searches Firebase (/api/v1/inventory/materials) when online,
 *   and searches locally cached materials when offline.
 * - Allows adding a non-existing material (with unit selection), saving it to Firebase
 *   and local cache, and immediately selecting it for the shift.
 * - Bottom: Active list of tapped materials, each with its own Stepper to adjust quantity.
 */
export function Step3Materials({ itemsUsed = [], onChangeItemsUsed }) {
  const [searchQuery, setSearchQuery] = useState('');
  const [catalogResults, setCatalogResults] = useState(() =>
    getCachedMaterials()
  );
  const [searching, setSearching] = useState(false);
  const [searchSource, setSearchSource] = useState('offline-cache');
  const [showAllCatalog, setShowAllCatalog] = useState(false);

  // New material creation state
  const [showAddForm, setShowAddForm] = useState(false);
  const [newMaterialName, setNewMaterialName] = useState('');
  const [newMaterialUnit, setNewMaterialUnit] = useState('Units');
  const [creatingMaterial, setCreatingMaterial] = useState(false);

  // Sync & search materials (online Firebase + offline cache fallback)
  useEffect(() => {
    let isMounted = true;

    // Immediate local filter for instant feedback while typing
    const immediateLocal = filterMaterialsLocally(
      getCachedMaterials(),
      searchQuery
    );
    setCatalogResults(immediateLocal);

    const timer = setTimeout(async () => {
      setSearching(true);
      const { source, items } = await searchMaterialsHybrid(searchQuery);
      if (!isMounted) return;
      setSearchSource(source);
      setCatalogResults(items);
      setSearching(false);
    }, 220);

    return () => {
      isMounted = false;
      clearTimeout(timer);
    };
  }, [searchQuery]);

  const isItemSelected = (id) => itemsUsed.some((item) => item.id === id);

  const handleToggleCatalogItem = (material) => {
    const matId = material.id || material.item_id;
    if (isItemSelected(matId)) {
      onChangeItemsUsed(itemsUsed.filter((item) => item.id !== matId));
    } else {
      onChangeItemsUsed([
        ...itemsUsed,
        {
          id: matId,
          sku: material.sku || String(matId).toUpperCase(),
          name: material.name,
          unit: material.unit || 'Units',
          quantity: 1,
        },
      ]);
    }
  };

  const handleQuantityChange = (id, nextQuantity) => {
    if (nextQuantity <= 0) {
      onChangeItemsUsed(itemsUsed.filter((item) => item.id !== id));
      return;
    }

    onChangeItemsUsed(
      itemsUsed.map((item) =>
        item.id === id ? { ...item, quantity: nextQuantity } : item
      )
    );
  };

  const exactMatchExists = catalogResults.some(
    (item) =>
      item.name?.trim().toLowerCase() === searchQuery.trim().toLowerCase()
  );

  const handleOpenAddMaterial = (prefillName = '') => {
    setNewMaterialName(prefillName || searchQuery.trim());
    setNewMaterialUnit('Units');
    setShowAddForm(true);
  };

  const handleCreateNewMaterial = async (e) => {
    e.preventDefault();
    const trimmedName = newMaterialName.trim();
    if (!trimmedName || creatingMaterial) return;

    try {
      setCreatingMaterial(true);
      const { item } = await createMaterialHybrid({
        name: trimmedName,
        unit: newMaterialUnit,
      });

      const matId = item.id || item.item_id;

      // Refresh visible catalog list
      setCatalogResults(
        filterMaterialsLocally(getCachedMaterials(), searchQuery)
      );

      // Automatically add the newly created material to itemsUsed if not already selected
      if (!isItemSelected(matId)) {
        onChangeItemsUsed([
          ...itemsUsed,
          {
            id: matId,
            sku: item.sku || String(matId).toUpperCase(),
            name: item.name,
            unit: item.unit || newMaterialUnit,
            quantity: 1,
          },
        ]);
      }

      setShowAddForm(false);
      setNewMaterialName('');
      setSearchQuery('');
    } finally {
      setCreatingMaterial(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-end justify-between gap-3">
        <div>
          <span className="text-xs font-black uppercase tracking-widest text-primary">
            Step 3 of 5
          </span>
          <h2 className="text-2xl sm:text-3xl font-black uppercase tracking-tight text-white mt-1">
            Inventory Used
          </h2>
        </div>

        {/* <button
          type="button"
          onClick={() => handleOpenAddMaterial(searchQuery)}
          className="
            min-h-touch px-4 py-3 rounded-xl
            bg-primary text-black border-2 border-white
            inline-flex items-center gap-2 shrink-0
            font-black text-sm uppercase tracking-wider
            shadow-hard-sm hover:opacity-95 active:scale-95 transition
            focus:outline-none focus:ring-4 focus:ring-white
          "
        >
          <Plus className="w-5 h-5 stroke-[3]" aria-hidden="true" />
          <span>New Material</span>
        </button> */}
      </div>

      {/* Research / Search Material Input */}
      <section className="space-y-2">

        <div className="relative flex items-center">
          <Search
            className="w-6 h-6 text-primary stroke-[2.75] absolute left-4 pointer-events-none"
            aria-hidden="true"
          />
          <input
            id="material-search-input"
            type="search"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search pipe, solenoid, fitting..."
            className="
              w-full min-h-touch-lg pl-13 pr-12 py-3.5 rounded-xl
              bg-black text-white text-lg sm:text-xl font-bold
              border-4 border-white
              placeholder:text-neutral-500
              focus:outline-none focus:border-primary focus:ring-4 focus:ring-primary
            "
            style={{ paddingLeft: '3.25rem' }}
          />
          {searchQuery && (
            <button
              type="button"
              onClick={() => setSearchQuery('')}
              aria-label="Clear material search"
              className="w-10 h-10 rounded-lg bg-dark-surface text-white border border-white flex items-center justify-center absolute right-3 hover:bg-safety-red"
            >
              <X className="w-5 h-5 stroke-[2.75]" aria-hidden="true" />
            </button>
          )}
        </div>
      </section>

      {/* Inline "Add New Material" Form */}
      {showAddForm && (
        <form
          onSubmit={handleCreateNewMaterial}
          className="bg-dark-surface border-4 border-primary rounded-2xl p-5 space-y-4 shadow-hard"
        >
          <div className="flex items-center justify-between border-b-2 border-dark-border pb-3">
            <div>
              <span className="text-xs font-black uppercase tracking-widest text-primary">
                New Catalog Item
              </span>
            </div>
            <button
              type="button"
              onClick={() => setShowAddForm(false)}
              className="w-10 h-10 rounded-lg bg-black text-white border-2 border-white flex items-center justify-center"
              aria-label="Close add material form"
            >
              <X className="w-5 h-5 stroke-[2.75]" aria-hidden="true" />
            </button>
          </div>

          <div>
            <label
              htmlFor="custom-material-name"
              className="block text-xs font-black uppercase tracking-wider text-neutral-300 mb-1.5"
            >
              Material Name
            </label>
            <input
              id="custom-material-name"
              type="text"
              required
              value={newMaterialName}
              onChange={(e) => setNewMaterialName(e.target.value)}
              placeholder="e.g. 32mm Poly Tee"
              className="
                w-full min-h-touch px-4 py-3 rounded-xl
                bg-black text-white text-lg font-bold
                border-4 border-white
                placeholder:text-neutral-500
                focus:outline-none focus:border-primary
              "
            />
          </div>

          <div>
            <span className="block text-xs font-black uppercase tracking-wider text-neutral-300 mb-1.5">
              Unit of Measure
            </span>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
              {UNIT_OPTIONS.map((unitOpt) => {
                const active = newMaterialUnit === unitOpt;
                return (
                  <button
                    key={unitOpt}
                    type="button"
                    onClick={() => setNewMaterialUnit(unitOpt)}
                    className={`min-h-touch px-3 py-2 rounded-xl font-black text-sm uppercase tracking-wider border-2 transition-colors ${
                      active
                        ? 'bg-primary text-black border-white'
                        : 'bg-black text-white border-dark-border hover:border-white'
                    }`}
                  >
                    {unitOpt}
                  </button>
                );
              })}
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3 pt-1">
            <FatButton
              type="button"
              variant="secondary"
              onClick={() => setShowAddForm(false)}
              disabled={creatingMaterial}
            >
              Cancel
            </FatButton>
            <FatButton
              type="submit"
              variant="primary"
              icon={Plus}
              disabled={creatingMaterial || !newMaterialName.trim()}
            >
              {creatingMaterial ? 'Adding...' : 'Add & Select'}
            </FatButton>
          </div>
        </form>
      )}

      {/* Top Section: Matching / Quick-Add Material QuickChips */}
      <section className="space-y-3">
        <div className="flex items-center justify-between gap-2">
          <div className="text-sm font-black uppercase tracking-widest text-neutral-300">
            {searchQuery.trim()
              ? `Matching Materials (${catalogResults.length})`
              : showAllCatalog
                ? `All Field Stock (${catalogResults.length})`
                : `Quick-Add Field Stock (Top ${Math.min(QUICK_PICK_LIMIT, catalogResults.length)})`}
          </div>

          {!searchQuery.trim() && catalogResults.length > QUICK_PICK_LIMIT && (
            <button
              type="button"
              onClick={() => setShowAllCatalog((prev) => !prev)}
              className="
                px-3 py-1.5 rounded-lg
                bg-dark-surface text-primary border-2 border-primary
                inline-flex items-center gap-1.5
                font-black text-xs uppercase tracking-wider
                hover:bg-primary hover:text-black transition-colors
              "
            >
              {showAllCatalog ? (
                <>
                  <span>Show Top {QUICK_PICK_LIMIT}</span>
                  <ChevronUp className="w-4 h-4 stroke-[3]" aria-hidden="true" />
                </>
              ) : (
                <>
                  <span>Show All ({catalogResults.length})</span>
                  <ChevronDown className="w-4 h-4 stroke-[3]" aria-hidden="true" />
                </>
              )}
            </button>
          )}
        </div>

        {catalogResults.length === 0 ? (
          <div className="p-6 rounded-2xl bg-dark-surface border-2 border-dark-border text-center space-y-3">
            <p className="text-base font-extrabold uppercase tracking-wide text-neutral-400">
              No matching materials found for &ldquo;{searchQuery.trim()}&rdquo;.
            </p>
            {!showAddForm && (
              <FatButton
                type="button"
                variant="primary"
                icon={Plus}
                onClick={() => handleOpenAddMaterial(searchQuery.trim())}
              >
                Add &ldquo;{searchQuery.trim()}&rdquo; as New Material
              </FatButton>
            )}
          </div>
        ) : (
          <div className="max-h-64 sm:max-h-72 overflow-y-auto pr-1 rounded-xl">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              {(searchQuery.trim() || showAllCatalog
                ? catalogResults
                : catalogResults.slice(0, QUICK_PICK_LIMIT)
              ).map((material) => {
                const matId = material.id || material.item_id;
                return (
                  <QuickChip
                    key={matId}
                    label={material.name}
                    icon={PackagePlus}
                    selected={isItemSelected(matId)}
                    onClick={() => handleToggleCatalogItem(material)}
                    className="w-full justify-start"
                  />
                );
              })}
            </div>
          </div>
        )}
      </section>

      {/* Bottom Section: Active Materials List with Individual Steppers */}
      <section className="space-y-4 pt-2 border-t-4 border-dark-border">
        <div className="flex items-center justify-between">
          <h3 className="text-base font-black uppercase tracking-widest text-primary">
            Selected Materials ({itemsUsed.length})
          </h3>
        </div>

        {itemsUsed.length === 0 ? (
          <div className="p-6 rounded-2xl bg-dark-surface border-2 border-dark-border text-center">
            <p className="text-lg font-extrabold uppercase tracking-wide text-neutral-400">
              No materials tapped yet. Search or tap any item above to add.
            </p>
          </div>
        ) : (
          <div className="space-y-4">
            {itemsUsed.map((item) => (
              <div
                key={item.id}
                className="bg-black border-2 border-white rounded-2xl p-4 space-y-3"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="text-xl font-black uppercase tracking-wide text-white">
                    {item.name}
                  </span>
                  <button
                    type="button"
                    onClick={() => handleQuantityChange(item.id, 0)}
                    aria-label={`Remove ${item.name}`}
                    className="
                      min-h-touch min-w-touch px-4 rounded-xl
                      bg-dark-surface text-safety-red border-2 border-safety-red
                      inline-flex items-center justify-center gap-2
                      font-black text-sm uppercase tracking-wider
                      touch-manipulation hover:bg-safety-red hover:text-white
                    "
                  >
                    <Trash2 className="w-6 h-6 stroke-[2.75]" aria-hidden="true" />
                    <span>Remove</span>
                  </button>
                </div>

                <Stepper
                  label={`${item.name} Quantity`}
                  value={item.quantity}
                  onChange={(val) => handleQuantityChange(item.id, val)}
                  step={1}
                  min={0}
                  max={500}
                  unit={item.unit}
                />
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

export default Step3Materials;


import React, { useRef, useState } from 'react';
import { motion, useMotionValue, useTransform } from 'framer-motion';
import {
  ChevronsRight,
  CheckCircle2,
  CloudOff,
  Clock,
  FolderKanban,
  Package,
  FileText,
  Camera,
  RotateCcw,
} from 'lucide-react';
import { submitShiftHybrid } from '../../lib/offlineQueue';
import { useAuth } from '../../context/AuthContext';
import { FatButton } from '../../components/ui/FatButton';

const HANDLE_SIZE = 68; // >= 60px Fat Finger rule

/**
 * Step 5: Review & Submit (Hybrid Offline-First)
 * - Displays high-contrast summary of Project, Time, Items, Photo, and Notes.
 * - Uses framer-motion drag slider ("Swipe to Log") to prevent accidental field taps.
 * - When online: sends POST /api/v1/shifts to FastAPI backend (saves to `shifts`,
 *   deducts Ute inventory stock, and dispatches low-stock Telegram alerts).
 * - When offline (or backend unreachable): queues shift locally in storage and
 *   automatically syncs when connectivity returns.
 */
export function Step5Review({
  selectedProject,
  hoursByCategory = {},
  itemsUsed = [],
  photo,
  notes = '',
  onReset,
}) {
  const { currentUser } = useAuth();
  const trackRef = useRef(null);
  // Stable per-review id so a retried swipe never creates duplicate shifts.
  const submissionIdRef = useRef(
    `sub_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`
  );
  const totalHours = Object.values(hoursByCategory).reduce(
    (sum, h) => sum + (Number(h) || 0),
    0
  );

  const [submitting, setSubmitting] = useState(false);
  const [submitStatus, setSubmitStatus] = useState(null); // 'online-success' | 'offline-queued'
  const [error, setError] = useState('');

  // Motion values for the Swipe-to-Log slider
  const x = useMotionValue(0);
  const backgroundOpacity = useTransform(x, [0, 220], [1, 0.2]);

  const handleSubmitShiftLog = async () => {
    if (submitting || submitStatus) return;

    setSubmitting(true);
    setError('');

    try {
      const result = await submitShiftHybrid({
        selectedProject,
        hoursByCategory,
        itemsUsed,
        photo,
        notes,
        currentUser,
        submissionId: submissionIdRef.current,
      });

      setSubmitStatus(result.status);
    } catch (err) {
      console.error('[Step5Review] Failed to log shift:', err);
      setError(err.message || 'Failed to save shift log. Please swipe again.');
      x.set(0);
    } finally {
      setSubmitting(false);
    }
  };

  const handleDragEnd = () => {
    if (submitting || submitStatus) return;

    const trackWidth = trackRef.current ? trackRef.current.offsetWidth : 300;
    const maxDrag = Math.max(0, trackWidth - HANDLE_SIZE - 8);
    const threshold = maxDrag * 0.72;

    if (x.get() >= threshold) {
      x.set(maxDrag);
      handleSubmitShiftLog();
    } else {
      x.set(0);
    }
  };

  // Confirmation Screen once logged
  if (submitStatus) {
    const isOfflineQueued = submitStatus === 'offline-queued';

    return (
      <div className="py-8 space-y-6 text-center">
        <div
          className={`p-8 rounded-2xl border-4 shadow-hard space-y-4 ${
            isOfflineQueued
              ? 'bg-primary text-black border-white'
              : 'bg-safety-green text-black border-white'
          }`}
        >
          {isOfflineQueued ? (
            <CloudOff className="w-20 h-20 mx-auto stroke-[2.75]" aria-hidden="true" />
          ) : (
            <CheckCircle2 className="w-20 h-20 mx-auto stroke-[2.75]" aria-hidden="true" />
          )}

          <h2 className="text-3xl sm:text-4xl font-black uppercase tracking-tight">
            {isOfflineQueued ? 'Saved Locally' : 'Shift Logged Successfully'}
          </h2>

          <p className="text-lg font-extrabold uppercase tracking-wide">
            {isOfflineQueued
              ? 'Queued in device storage — will sync automatically when signal returns.'
              : 'Your shift and material records have been synced to LaTrobe HQ.'}
          </p>
        </div>

        {onReset && (
          <FatButton
            type="button"
            variant="secondary"
            icon={RotateCcw}
            onClick={onReset}
            className="min-h-touch-lg text-2xl"
          >
            Log Another Shift
          </FatButton>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <span className="text-xs font-black uppercase tracking-widest text-primary">
          Step 5 of 5 • Final Check
        </span>
        <h2 className="text-2xl sm:text-3xl font-black uppercase tracking-tight text-white mt-1">
          Review & Swipe to Log
        </h2>
      </div>

      {/* High-Contrast Shift Summary Card */}
      <div className="bg-dark-surface border-4 border-white rounded-2xl divide-y-2 divide-dark-border overflow-hidden shadow-hard">
        {/* Project Row */}
        <div className="p-4 flex items-start gap-3">
          <FolderKanban className="w-7 h-7 text-primary shrink-0 stroke-[2.5] mt-0.5" aria-hidden="true" />
          <div className="min-w-0 flex-1">
            <span className="block text-xs font-black uppercase tracking-widest text-neutral-400">
              Project
            </span>
            <span className="block text-xl font-black uppercase text-white truncate">
              {selectedProject?.name || 'No Project Selected'}
            </span>
            {selectedProject?.location && (
              <span className="block text-sm font-bold uppercase text-primary">
                {selectedProject.location}
              </span>
            )}
          </div>
        </div>

        {/* Time Breakdown Row */}
        <div className="p-4 flex items-start gap-3">
          <Clock className="w-7 h-7 text-primary shrink-0 stroke-[2.5] mt-0.5" aria-hidden="true" />
          <div className="flex-1 space-y-2">
            <div className="flex items-center justify-between gap-2">
              <span className="block text-xs font-black uppercase tracking-widest text-neutral-400">
                Time Logged
              </span>
              <span className="text-2xl font-black uppercase text-white tabular-nums">
                {totalHours.toFixed(2)} HRS
              </span>
            </div>
            <ul className="space-y-1.5">
              {Object.entries(hoursByCategory)
                .filter(([, h]) => Number(h) > 0)
                .map(([cat, h]) => (
                  <li
                    key={cat}
                    className="flex items-center justify-between bg-black px-3 py-2 rounded-lg border border-dark-border"
                  >
                    <span className="font-extrabold uppercase text-white">{cat}</span>
                    <span className="font-black text-primary uppercase tabular-nums">
                      {Number(h).toFixed(2)} HRS
                    </span>
                  </li>
                ))}
            </ul>
          </div>
        </div>

        {/* Materials Row */}
        <div className="p-4 flex items-start gap-3">
          <Package className="w-7 h-7 text-primary shrink-0 stroke-[2.5] mt-0.5" aria-hidden="true" />
          <div className="flex-1 space-y-2">
            <span className="block text-xs font-black uppercase tracking-widest text-neutral-400">
              Materials Used ({itemsUsed.length})
            </span>
            {itemsUsed.length === 0 ? (
              <span className="block text-base font-bold uppercase text-neutral-400">
                None
              </span>
            ) : (
              <ul className="space-y-1.5">
                {itemsUsed.map((item) => (
                  <li
                    key={item.id}
                    className="flex items-center justify-between bg-black px-3 py-2 rounded-lg border border-dark-border"
                  >
                    <span className="font-extrabold uppercase text-white">
                      {item.name}
                    </span>
                    <span className="font-black text-primary uppercase tabular-nums">
                      {item.quantity} {item.unit}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>

        {/* Photo & Notes Row */}
        <div className="p-4 flex items-start gap-3">
          <FileText className="w-7 h-7 text-primary shrink-0 stroke-[2.5] mt-0.5" aria-hidden="true" />
          <div className="flex-1 space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-xs font-black uppercase tracking-widest text-neutral-400">
                Field Notes & Evidence
              </span>
              {photo && (
                <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-safety-green text-black text-xs font-black uppercase">
                  <Camera className="w-4 h-4 stroke-[2.75]" aria-hidden="true" />
                  Photo Ready
                </span>
              )}
            </div>
            <p className="text-base font-bold text-white whitespace-pre-wrap">
              {notes.trim() ? notes : 'No voice or text notes added.'}
            </p>
          </div>
        </div>
      </div>

      {error && (
        <div
          role="alert"
          className="p-4 rounded-xl bg-safety-red text-white border-2 border-white font-black uppercase text-center"
        >
          {error}
        </div>
      )}

      {/* Framer Motion "Swipe to Log" Slider */}
      <div className="pt-2">
        <div
          ref={trackRef}
          className="relative w-full h-20 rounded-2xl bg-black border-4 border-primary p-1 flex items-center overflow-hidden select-none shadow-hard"
        >
          {/* Background Track Label */}
          <motion.div
            style={{ opacity: backgroundOpacity }}
            className="absolute inset-0 flex items-center justify-center pointer-events-none pl-14"
          >
            <span className="text-xl sm:text-2xl font-black uppercase tracking-widest text-primary">
              {submitting ? 'Logging Shift...' : 'Swipe to Log Shift >>>'}
            </span>
          </motion.div>

          {/* Draggable Thumb (>= 68x68px Touch Target) */}
          <motion.div
            role="slider"
            aria-label="Swipe right to log shift"
            aria-valuemin={0}
            aria-valuemax={100}
            tabIndex={0}
            drag={submitting ? false : 'x'}
            dragConstraints={trackRef}
            dragElastic={0.05}
            dragMomentum={false}
            onDragEnd={handleDragEnd}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                handleSubmitShiftLog();
              }
            }}
            style={{ x, width: HANDLE_SIZE, height: HANDLE_SIZE }}
            whileTap={{ scale: 0.96 }}
            className="
              relative z-10 rounded-xl
              bg-primary text-black border-2 border-white
              flex items-center justify-center
              cursor-grab active:cursor-grabbing touch-none
              focus:outline-none focus:ring-4 focus:ring-white
            "
          >
            <ChevronsRight className="w-10 h-10 stroke-[3]" aria-hidden="true" />
          </motion.div>
        </div>
      </div>
    </div>
  );
}

export default Step5Review;

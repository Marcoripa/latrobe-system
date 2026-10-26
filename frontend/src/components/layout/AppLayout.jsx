import React, { useEffect, useState } from 'react';
import { Outlet } from 'react-router-dom';
import { Wifi, WifiOff, User, LogOut, RefreshCw, CloudUpload } from 'lucide-react';
import { useAuth } from '../../context/AuthContext';
import {
  getQueueState,
  initOfflineQueueAutoSync,
  subscribeToShiftQueue,
  syncQueuedShifts,
} from '../../lib/offlineQueue';

export function AppLayout({ children }) {
  const { currentUser, logout } = useAuth();
  const [role, setRole] = useState('operator');
  const [isOnline, setIsOnline] = useState(
    typeof navigator !== 'undefined' ? navigator.onLine : true
  );
  const [queueState, setQueueState] = useState(() => getQueueState());

  useEffect(() => {
    initOfflineQueueAutoSync();
    const unsubscribeQueue = subscribeToShiftQueue(setQueueState);

    const handleOnline = () => setIsOnline(true);
    const handleOffline = () => setIsOnline(false);

    window.addEventListener('online', handleOnline);
    window.addEventListener('offline', handleOffline);

    return () => {
      unsubscribeQueue();
      window.removeEventListener('online', handleOnline);
      window.removeEventListener('offline', handleOffline);
    };
  }, []);

  useEffect(() => {
    if (!currentUser) return;

    // Pass `true` to getIdTokenResult(true) if you want to force-refresh the token from Firebase
    currentUser.getIdTokenResult().then((idTokenResult) => {
      setRole(idTokenResult.claims.role || 'operator');
    });
  }, [currentUser]);

  const operatorName =
    currentUser?.displayName ||
    currentUser?.email?.split('@')[0] ||
    'Field Operator';

  return (
    <div className="min-h-screen bg-dark text-white flex flex-col">
      {/* Thick High-Contrast Top Bar */}
      <header className="bg-black border-b-4 border-white px-4 py-3 sm:px-6">
        <div className="max-w-3xl mx-auto flex items-center justify-between gap-3">
          {/* Operator Identity */}
          <div className="flex items-center gap-3 min-w-0">
            <div className="w-12 h-12 rounded-xl bg-dark-surface border-2 border-primary flex items-center justify-center shrink-0">
              <User className="w-7 h-7 text-primary stroke-[2.75]" aria-hidden="true" />
            </div>
            <div className="min-w-0">
              <span className="block text-xs font-black uppercase tracking-widest text-neutral-400">
                {role}
              </span>
              <span className="block text-lg sm:text-xl font-black uppercase tracking-wide text-white truncate">
                {operatorName}
              </span>
            </div>
          </div>

          {/* Right Controls: Connectivity Badge & Logout */}
          <div className="flex items-center gap-3 shrink-0">
            <button
              type="button"
              onClick={logout}
              aria-label="Sign Out"
              title="Sign Out"
              className="
                min-w-touch min-h-touch px-4 rounded-xl
                bg-dark-surface text-white border-2 border-white
                hover:bg-safety-red hover:border-white
                inline-flex items-center justify-center
                touch-manipulation transition-colors
                focus:outline-none focus:ring-4 focus:ring-primary
              "
            >
              <LogOut className="w-6 h-6 stroke-[2.75]" aria-hidden="true" />
            </button>
          </div>
        </div>
      </header>

      {/* Prominent Offline Mode Banner */}
      {!isOnline && (
        <div
          role="alert"
          className="bg-primary text-black border-b-4 border-black px-4 py-3"
        >
          <div className="max-w-3xl mx-auto flex items-center justify-center gap-3 text-center">
            <WifiOff className="w-7 h-7 stroke-[3] shrink-0" aria-hidden="true" />
            <span className="font-black text-base sm:text-lg uppercase tracking-wider">
              Offline Mode - Data will sync later
              {queueState.pendingCount > 0
                ? ` (${queueState.pendingCount} queued)`
                : ''}
            </span>
          </div>
        </div>
      )}

      {/* Queued Shifts Auto-Sync Banner (When Online & Queue Non-Empty) */}
      {isOnline && queueState.pendingCount > 0 && (
        <div
          role="status"
          className="bg-dark-surface text-white border-b-4 border-primary px-4 py-2.5"
        >
          <div className="max-w-3xl mx-auto flex items-center justify-between gap-3">
            <div className="flex items-center gap-2.5">
              {queueState.isSyncing ? (
                <RefreshCw
                  className="w-5 h-5 text-primary stroke-[2.75] animate-spin shrink-0"
                  aria-hidden="true"
                />
              ) : (
                <CloudUpload
                  className="w-5 h-5 text-primary stroke-[2.75] shrink-0"
                  aria-hidden="true"
                />
              )}
              <span className="font-black text-sm sm:text-base uppercase tracking-wider">
                {queueState.isSyncing
                  ? `Syncing ${queueState.pendingCount} queued shift(s) to HQ...`
                  : `${queueState.pendingCount} offline shift(s) waiting to sync`}
              </span>
            </div>

            {!queueState.isSyncing && (
              <button
                type="button"
                onClick={() => syncQueuedShifts()}
                className="px-3 py-1.5 rounded-lg bg-primary text-black font-black text-xs uppercase tracking-wider border-2 border-white hover:opacity-90"
              >
                Sync Now
              </button>
            )}
          </div>
        </div>
      )}

      {/* Main Authenticated Content Area */}
      <main className="flex-1 w-full max-w-3xl mx-auto p-4 sm:p-6">
        {children || <Outlet />}
      </main>
    </div>
  );
}

export default AppLayout;

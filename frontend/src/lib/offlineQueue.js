import { onAuthStateChanged } from 'firebase/auth';
import { auth } from './firebase';

export const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'
).replace(/\/$/, '');

const SHIFT_QUEUE_STORAGE_KEY = 'latrobe_offline_shift_queue_v1';
const PROJECTS_CACHE_STORAGE_KEY = 'latrobe_cached_projects_v1';
const ONLINE_TIMEOUT_MS = 4000;

let isSyncing = false;
let autoSyncInitialized = false;
let lastSyncedCount = 0;
let lastSyncAt = null;
const listeners = new Set();

function generateClientId() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `shift_${crypto.randomUUID()}`;
  }
  return `shift_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
}

/**
 * Read the persisted offline shift queue from localStorage.
 */
export function getShiftQueue() {
  if (typeof window === 'undefined') return [];
  try {
    const raw = window.localStorage.getItem(SHIFT_QUEUE_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch (err) {
    console.error('[OfflineQueue] Failed to read shift queue from storage:', err);
    return [];
  }
}

function saveShiftQueue(queue) {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(SHIFT_QUEUE_STORAGE_KEY, JSON.stringify(queue));
  } catch (err) {
    console.error('[OfflineQueue] Failed to save shift queue to storage:', err);
  }
  notifyListeners();
}

function notifyListeners() {
  const snapshot = getQueueState();
  listeners.forEach((listener) => {
    try {
      listener(snapshot);
    } catch (err) {
      console.error('[OfflineQueue] Listener error:', err);
    }
  });
}

export function getQueueState() {
  const queue = getShiftQueue();
  return {
    queue,
    pendingCount: queue.length,
    isSyncing,
    lastSyncedCount,
    lastSyncAt,
  };
}

/**
 * Subscribe to offline queue state changes (pending count, syncing status, etc.).
 */
export function subscribeToShiftQueue(listener) {
  listeners.add(listener);
  listener(getQueueState());
  return () => {
    listeners.delete(listener);
  };
}

/**
 * Build the FastAPI ShiftLogCreate payload from the frontend Wizard state.
 */
export function buildShiftApiPayload({
  selectedProject,
  hours,
  category,
  itemsUsed = [],
  photo,
  notes = '',
  currentUser,
  clientShiftId,
}) {
  const id = clientShiftId || generateClientId();
  return {
    client_shift_id: id,
    project_id: selectedProject?.id || 'unassigned',
    operator_id: currentUser?.uid || 'anonymous-operator',
    ute_id: selectedProject?.ute_id || selectedProject?.uteId || null,
    hours: Number(hours),
    category: category || 'On-Site',
    items_used: itemsUsed.map((item) => ({
      item_id: String(item.id || item.item_id),
      sku: String(item.sku || item.id || item.item_id).toUpperCase(),
      name: String(item.name || item.id),
      quantity: Number(item.quantity),
      ...(item.unit_price != null ? { unit_price: Number(item.unit_price) } : {}),
    })),
    notes: notes.trim() || null,
    photo_url: photo?.name ? `local-capture://${photo.name}` : null,
  };
}

/**
 * Add a shift payload to the persistent local offline queue.
 */
export function enqueueShift(payload, meta = {}) {
  const queue = getShiftQueue();
  const existingIndex = queue.findIndex(
    (entry) => entry.client_shift_id === payload.client_shift_id
  );

  const queuedItem = {
    client_shift_id: payload.client_shift_id,
    payload,
    meta: {
      projectName: meta.projectName || payload.project_id,
      queuedAt: new Date().toISOString(),
      attempts: 0,
      lastError: null,
      ...meta,
    },
  };

  if (existingIndex >= 0) {
    queue[existingIndex] = queuedItem;
  } else {
    queue.push(queuedItem);
  }

  saveShiftQueue(queue);
  return queuedItem;
}

/**
 * Send a single shift payload to the FastAPI backend (`POST /api/v1/shifts`).
 */
async function postShiftToBackend(payload, timeoutMs = ONLINE_TIMEOUT_MS) {
  const user = auth.currentUser;
  if (!user) {
    const err = new Error('User is not authenticated.');
    err.isAuthError = true;
    throw err;
  }

  const token = await user.getIdToken();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(`${API_BASE_URL}/api/v1/shifts`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });

    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try {
        const errBody = await response.json();
        detail =
          typeof errBody.detail === 'string'
            ? errBody.detail
            : JSON.stringify(errBody.detail || errBody);
      } catch {
        // ignore JSON parse failure
      }
      const httpErr = new Error(detail);
      httpErr.status = response.status;
      throw httpErr;
    }

    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Hybrid Offline-First Shift Submission:
 * - When online, attempts to POST directly to the FastAPI backend (`/api/v1/shifts`)
 *   so Ute inventory stock is deducted and Telegram low-stock alerts trigger immediately.
 * - When offline (or if the network/backend is unreachable), queues the shift in
 *   localStorage and automatically syncs when connectivity returns.
 */
export async function submitShiftHybrid({
  selectedProject,
  hoursByCategory,
  hours,
  category,
  itemsUsed = [],
  photo,
  notes = '',
  currentUser,
  submissionId,
}) {
  // One shift record per work type with hours (Travel / On-Site / Shed/Prep).
  // Materials, notes and photo are attached to the first record only so stock
  // is never deducted twice. Ids are stable per submission => retries are idempotent.
  const breakdown = hoursByCategory
    ? Object.entries(hoursByCategory).filter(([, h]) => Number(h) > 0)
    : [[category || 'On-Site', hours]];
  const baseId = submissionId || generateClientId();

  const payloads = breakdown.map(([cat, catHours], index) =>
    buildShiftApiPayload({
      selectedProject,
      hours: catHours,
      category: cat,
      itemsUsed: index === 0 ? itemsUsed : [],
      photo: index === 0 ? photo : null,
      notes: index === 0 ? notes : '',
      currentUser,
      clientShiftId: `${baseId}_${String(cat).toLowerCase().replace(/[^a-z0-9]+/g, '-')}`,
    })
  );

  const meta = {
    projectName: selectedProject?.name || payloads[0]?.project_id,
  };

  const isOnlineNow =
    typeof navigator !== 'undefined' ? navigator.onLine : true;

  if (!isOnlineNow) {
    payloads.forEach((p) => enqueueShift(p, meta));
    return { status: 'offline-queued' };
  }

  let queuedAny = false;
  for (let i = 0; i < payloads.length; i += 1) {
    const payload = payloads[i];

    // Once one record had to be queued, queue the rest to preserve order.
    if (queuedAny) {
      enqueueShift(payload, meta);
      continue;
    }

    try {
      await postShiftToBackend(payload, ONLINE_TIMEOUT_MS);
    } catch (err) {
      const isClientValidationError =
        err.status &&
        err.status >= 400 &&
        err.status < 500 &&
        err.status !== 408 &&
        err.status !== 429;

      if (isClientValidationError) {
        throw err;
      }

      console.warn(
        '[OfflineQueue] Backend unreachable or timed out; queuing shift locally for auto-sync:',
        err.message || err
      );
      enqueueShift(payload, {
        ...meta,
        lastError: err.message || 'Network unavailable',
      });
      queuedAny = true;
    }
  }

  if (!queuedAny && getShiftQueue().length > 0) {
    // Opportunistically flush any older queued items in the background
    syncQueuedShifts().catch(() => {});
  }

  return { status: queuedAny ? 'offline-queued' : 'online-success' };
}

/**
 * Flush all locally queued shifts to the FastAPI backend in FIFO order.
 * Safe to call repeatedly; concurrent calls are deduplicated via `isSyncing`.
 */
export async function syncQueuedShifts() {
  if (isSyncing) return { synced: 0, remaining: getShiftQueue().length };
  if (typeof navigator !== 'undefined' && !navigator.onLine) {
    return { synced: 0, remaining: getShiftQueue().length };
  }
  if (!auth.currentUser) {
    return { synced: 0, remaining: getShiftQueue().length };
  }

  const initialQueue = getShiftQueue();
  if (initialQueue.length === 0) {
    return { synced: 0, remaining: 0 };
  }

  isSyncing = true;
  notifyListeners();

  let syncedCount = 0;
  const remainingQueue = [...initialQueue];

  try {
    for (let i = 0; i < initialQueue.length; i += 1) {
      if (typeof navigator !== 'undefined' && !navigator.onLine) {
        break;
      }

      const entry = initialQueue[i];
      try {
        await postShiftToBackend(entry.payload, 8000);
        syncedCount += 1;

        // Remove synced item from remainingQueue
        const idx = remainingQueue.findIndex(
          (q) => q.client_shift_id === entry.client_shift_id
        );
        if (idx !== -1) {
          remainingQueue.splice(idx, 1);
        }
        saveShiftQueue(remainingQueue);
      } catch (err) {
        console.warn(
          `[OfflineQueue] Failed to sync queued shift '${entry.client_shift_id}':`,
          err.message || err
        );

        const idx = remainingQueue.findIndex(
          (q) => q.client_shift_id === entry.client_shift_id
        );
        if (idx !== -1) {
          remainingQueue[idx] = {
            ...remainingQueue[idx],
            meta: {
              ...remainingQueue[idx].meta,
              attempts: (remainingQueue[idx].meta?.attempts || 0) + 1,
              lastError: err.message || 'Sync failed',
              lastAttemptAt: new Date().toISOString(),
            },
          };
          saveShiftQueue(remainingQueue);
        }

        // If network dropped or server is down, stop this batch and wait for next online event
        if (!err.status || err.status >= 500) {
          break;
        }
      }
    }

    if (syncedCount > 0) {
      lastSyncedCount = syncedCount;
      lastSyncAt = new Date().toISOString();
    }
  } finally {
    isSyncing = false;
    notifyListeners();
  }

  return { synced: syncedCount, remaining: remainingQueue.length };
}

/**
 * Initialize automatic background queue synchronization on:
 * 1. Browser `online` event
 * 2. Firebase Auth session ready while online
 * 3. Tab `visibilitychange` while online
 * 4. Periodic 30-second retry interval when queued shifts exist
 */
export function initOfflineQueueAutoSync() {
  if (autoSyncInitialized || typeof window === 'undefined') return;
  autoSyncInitialized = true;

  window.addEventListener('online', () => {
    syncQueuedShifts().catch((err) =>
      console.error('[OfflineQueue] Error syncing on online event:', err)
    );
  });

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && navigator.onLine) {
      syncQueuedShifts().catch(() => {});
    }
  });

  onAuthStateChanged(auth, (user) => {
    if (user && navigator.onLine && getShiftQueue().length > 0) {
      syncQueuedShifts().catch((err) =>
        console.error('[OfflineQueue] Error syncing after auth ready:', err)
      );
    }
  });

  window.setInterval(() => {
    if (navigator.onLine && auth.currentUser && getShiftQueue().length > 0) {
      syncQueuedShifts().catch(() => {});
    }
  }, 30000);
}

/**
 * Local Project Cache helpers for hybrid offline project loading.
 */
export function getCachedProjects() {
  if (typeof window === 'undefined') return [];
  try {
    const raw = window.localStorage.getItem(PROJECTS_CACHE_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export function setCachedProjects(projects) {
  if (typeof window === 'undefined' || !Array.isArray(projects)) return;
  try {
    window.localStorage.setItem(
      PROJECTS_CACHE_STORAGE_KEY,
      JSON.stringify(projects)
    );
  } catch {
    // ignore storage quota errors
  }
}

/**
 * Fetch active projects via the FastAPI backend (`GET /api/v1/projects?status=active`)
 * and update the local offline project cache.
 */
export async function fetchProjectsViaBackend() {
  const user = auth.currentUser;
  if (!user) {
    throw new Error('User is not authenticated.');
  }
  const token = await user.getIdToken();
  const response = await fetch(`${API_BASE_URL}/api/v1/projects?status=active`, {
    method: 'GET',
    headers: {
      Accept: 'application/json',
      Authorization: `Bearer ${token}`,
    },
  });
  if (!response.ok) {
    throw new Error(`Backend /api/v1/projects returned HTTP ${response.status}`);
  }
  const projects = await response.json();
  if (Array.isArray(projects)) {
    setCachedProjects(projects);
  }
  return projects;
}

/**
 * Create a new project via the FastAPI backend (`POST /api/v1/projects`)
 * and append it to the local offline project cache.
 */
export async function createProjectViaBackend({ name, client_name }) {
  const user = auth.currentUser;
  if (!user) {
    throw new Error('User is not authenticated.');
  }
  const token = await user.getIdToken();
  const response = await fetch(`${API_BASE_URL}/api/v1/projects`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({
      name: name.trim(),
      client_name: client_name.trim(),
    }),
  });

  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const errBody = await response.json();
      detail =
        typeof errBody.detail === 'string'
          ? errBody.detail
          : JSON.stringify(errBody.detail || errBody);
    } catch {
      // ignore parse error
    }
    throw new Error(detail);
  }

  const created = await response.json();
  const cached = getCachedProjects();
  if (!cached.some((p) => p.id === created.id)) {
    setCachedProjects([created, ...cached]);
  }
  return created;
}

const MATERIALS_CACHE_STORAGE_KEY = 'latrobe_cached_materials_v1';

export const DEFAULT_COMMON_MATERIALS = [
  { id: '19mm-pipe', item_id: '19mm-pipe', sku: '19MM-PIPE', name: '19mm Pipe', unit: 'Meters' },
  { id: '25mm-solenoid', item_id: '25mm-solenoid', sku: '25MM-SOLENOID', name: '25mm Solenoid', unit: 'Units' },
  { id: '25mm-elbow', item_id: '25mm-elbow', sku: '25MM-ELBOW', name: '25mm Elbow', unit: 'Units' }
];

/**
 * Retrieve cached materials from localStorage, always including DEFAULT_COMMON_MATERIALS.
 */
export function getCachedMaterials() {
  const byId = new Map(
    DEFAULT_COMMON_MATERIALS.map((item) => [item.id, { ...item }])
  );
  if (typeof window !== 'undefined') {
    try {
      const raw = window.localStorage.getItem(MATERIALS_CACHE_STORAGE_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed)) {
          parsed.forEach((item) => {
            const key = item.id || item.item_id;
            if (key) {
              byId.set(key, {
                id: key,
                item_id: key,
                sku: item.sku || String(key).toUpperCase(),
                name: item.name || key,
                unit: item.unit || 'Units',
              });
            }
          });
        }
      }
    } catch {
      // ignore storage read error
    }
  }
  return Array.from(byId.values());
}

/**
 * Merge newly discovered or created materials into localStorage cache.
 */
export function mergeCachedMaterials(newMaterials = []) {
  const existing = getCachedMaterials();
  const byId = new Map(existing.map((item) => [item.id, item]));
  newMaterials.forEach((item) => {
    const key = item.id || item.item_id;
    if (key) {
      byId.set(key, {
        id: key,
        item_id: key,
        sku: item.sku || String(key).toUpperCase(),
        name: item.name || key,
        unit: item.unit || 'Units',
      });
    }
  });
  const merged = Array.from(byId.values());
  if (typeof window !== 'undefined') {
    try {
      window.localStorage.setItem(
        MATERIALS_CACHE_STORAGE_KEY,
        JSON.stringify(merged)
      );
    } catch {
      // ignore storage write error
    }
  }
  return merged;
}

/**
 * Filter a list of materials locally by search query (used when offline or merging local cache).
 */
export function filterMaterialsLocally(materials, queryText = '') {
  const q = queryText.trim().toLowerCase();
  if (!q) return materials;
  const tokens = q.split(/\s+/).filter(Boolean);
  return materials.filter((item) => {
    const haystack = `${item.name || ''} ${item.sku || ''} ${item.id || ''}`.toLowerCase();
    return tokens.every((tok) => haystack.includes(tok));
  });
}

/**
 * Hybrid Material Search:
 * - When online: queries GET /api/v1/inventory/materials?q=... in Firebase,
 *   updates the local material cache, and returns matches.
 * - When offline (or backend unreachable): searches within locally cached items.
 */
export async function searchMaterialsHybrid(queryText = '') {
  const isOnlineNow =
    typeof navigator !== 'undefined' ? navigator.onLine : true;

  if (!isOnlineNow || !auth.currentUser) {
    return {
      source: 'offline-cache',
      items: filterMaterialsLocally(getCachedMaterials(), queryText),
    };
  }

  try {
    const token = await auth.currentUser.getIdToken();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), ONLINE_TIMEOUT_MS);

    try {
      const url = `${API_BASE_URL}/api/v1/inventory/materials?q=${encodeURIComponent(
        queryText.trim()
      )}`;
      const response = await fetch(url, {
        method: 'GET',
        headers: {
          Accept: 'application/json',
          Authorization: `Bearer ${token}`,
        },
        signal: controller.signal,
      });

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      const remoteItems = await response.json();
      if (Array.isArray(remoteItems)) {
        mergeCachedMaterials(remoteItems);
      }

      // Combine remote matches with any locally created offline items that match the query
      const localMatches = filterMaterialsLocally(
        getCachedMaterials(),
        queryText
      );
      const combinedById = new Map();
      (Array.isArray(remoteItems) ? remoteItems : []).forEach((item) => {
        const key = item.id || item.item_id;
        if (key) combinedById.set(key, item);
      });
      localMatches.forEach((item) => {
        if (!combinedById.has(item.id)) {
          combinedById.set(item.id, item);
        }
      });

      return {
        source: 'firebase',
        items: Array.from(combinedById.values()),
      };
    } finally {
      clearTimeout(timer);
    }
  } catch (err) {
    console.warn(
      '[OfflineQueue] Material search falling back to cached items:',
      err.message || err
    );
    return {
      source: 'offline-cache',
      items: filterMaterialsLocally(getCachedMaterials(), queryText),
    };
  }
}

/**
 * Create a new material item:
 * - Always saves immediately to the local materials cache so it works offline.
 * - When online, also persists it in Firebase via POST /api/v1/inventory/materials.
 */
export async function createMaterialHybrid({ name, unit = 'Units' }) {
  const cleanName = name.trim();
  const cleanUnit = unit.trim() || 'Units';
  const slug =
    cleanName
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-|-$/g, '') || `mat-${Date.now()}`;

  const localItem = {
    id: slug,
    item_id: slug,
    sku: slug.toUpperCase(),
    name: cleanName,
    unit: cleanUnit,
  };

  // Immediately cache locally so offline searches find it right away
  mergeCachedMaterials([localItem]);

  const isOnlineNow =
    typeof navigator !== 'undefined' ? navigator.onLine : true;

  if (!isOnlineNow || !auth.currentUser) {
    return { status: 'offline-cached', item: localItem };
  }

  try {
    const token = await auth.currentUser.getIdToken();
    const response = await fetch(`${API_BASE_URL}/api/v1/inventory/materials`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'application/json',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({
        name: cleanName,
        unit: cleanUnit,
      }),
    });

    if (response.ok) {
      const created = await response.json();
      mergeCachedMaterials([created]);
      return { status: 'online-created', item: created };
    }
  } catch (err) {
    console.warn(
      '[OfflineQueue] Saved new material to local cache (backend unreachable):',
      err.message || err
    );
  }

  return { status: 'offline-cached', item: localItem };
}



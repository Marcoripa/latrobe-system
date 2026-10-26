import { initializeApp } from 'firebase/app';
import { getAuth } from 'firebase/auth';
import {
  getFirestore,
  enableMultiTabIndexedDbPersistence,
} from 'firebase/firestore';

const firebaseConfig = {
  apiKey: import.meta.env.VITE_FIREBASE_API_KEY,
  authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN,
  projectId: import.meta.env.VITE_FIREBASE_PROJECT_ID,
  storageBucket: import.meta.env.VITE_FIREBASE_STORAGE_BUCKET,
  messagingSenderId: import.meta.env.VITE_FIREBASE_MESSAGING_SENDER_ID,
  appId: import.meta.env.VITE_FIREBASE_APP_ID,
};

// Initialize Firebase App
export const app = initializeApp(firebaseConfig);

// Initialize Auth and Firestore
export const auth = getAuth(app);
export const db = getFirestore(app);

// CRITICAL OFFLINE LOGIC: Enable multi-tab IndexedDB persistence so field operators
// can log shifts and inventory in zero-connectivity irrigation zones.
enableMultiTabIndexedDbPersistence(db).catch((err) => {
  if (err.code === 'failed-precondition') {
    // Multiple tabs open with conflicting persistence settings
    console.warn(
      '[Firebase Offline] Persistence failed-precondition: Multiple tabs open with conflicting configuration.',
      err
    );
  } else if (err.code === 'unimplemented') {
    // The current browser does not support all of the features required to enable persistence
    console.warn(
      '[Firebase Offline] Persistence unimplemented: Browser does not support IndexedDB persistence.',
      err
    );
  } else {
    console.error('[Firebase Offline] Unexpected persistence error:', err);
  }
});

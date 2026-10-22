/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        primary: {
          DEFAULT: '#eab308', // Safety Yellow (yellow-500)
          hover: '#ca8a04',   // yellow-600
          active: '#facc15',  // yellow-400
        },
        safety: {
          yellow: '#eab308',
          orange: '#f97316',  // High-visibility safety orange
          red: '#ef4444',     // Danger / Stop
          green: '#22c55e',   // Active / Success
        },
        dark: {
          DEFAULT: '#171717', // High-contrast neutral-900 background
          surface: '#262626', // Elevated cards/containers (neutral-800)
          border: '#404040',  // Distinct borders for outdoor glare (neutral-700)
          pure: '#000000',    // Pure black for maximum contrast
        },
      },
      minHeight: {
        'touch': '60px',      // "Fat Finger" rule minimum height
        'touch-lg': '72px',
      },
      minWidth: {
        'touch': '60px',      // "Fat Finger" rule minimum width
        'touch-lg': '72px',
      },
      boxShadow: {
        'hard': '0 4px 0 0 #000000',
        'hard-sm': '0 2px 0 0 #000000',
      },
    },
  },
  plugins: [],
};

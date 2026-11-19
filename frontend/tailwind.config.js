/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#070b12",
        panel: "#0d1420",
        edge: "#1c2a3d",
        lime: "#c6ff4a",
        cyan: "#4ad8ff",
        amber: "#ffb84a",
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular", "monospace"],
      },
      boxShadow: {
        glow: "0 0 40px rgba(74, 216, 255, 0.12)",
      },
    },
  },
  plugins: [],
};

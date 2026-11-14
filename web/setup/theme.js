// Applies a saved light/dark choice before the page is drawn, so it doesn't flash
// in the other theme first. Without a saved choice the page follows the system.
// A classic (blocking) script on purpose: setup.js is a module and runs too late.
try {
  const saved = localStorage.getItem("emotion-overlay.theme");
  if (saved === "light" || saved === "dark") document.documentElement.dataset.theme = saved;
} catch {
  // storage blocked: follow the system setting
}

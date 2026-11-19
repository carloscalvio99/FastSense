const CATEGORY_ICON = {
  Laptops: "💻",
  Smartphones: "📱",
  Audio: "🎧",
  Wearables: "⌚",
  Gaming: "🎮",
  Monitores: "🖥️",
  Almacenamiento: "💾",
  Redes: "📡",
  Accesorios: "🖱️",
  "Smart Home": "🏠",
};

export function categoryIcon(category) {
  return CATEGORY_ICON[category] || "📦";
}

const currency = new Intl.NumberFormat("es-CL", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});

export function formatPrice(value) {
  return currency.format(Number(value) || 0);
}

export function formatNumber(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return new Intl.NumberFormat("es-CL", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(Number(value));
}

export function timeAgo(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const seconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
  if (seconds < 60) return `hace ${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `hace ${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `hace ${hours}h`;
  return `hace ${Math.floor(hours / 24)}d`;
}

export const SOURCE_STYLE = {
  cache: "border-cyan/40 bg-cyan/10 text-cyan",
  svd: "border-lime/40 bg-lime/10 text-lime",
  fallback: "border-amber/40 bg-amber/10 text-amber",
};

export const SOURCE_LABEL = {
  cache: "Cache Redis",
  svd: "SVD · inferencia",
  fallback: "Fallback top:global",
};

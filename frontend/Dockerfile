# =====================================================================
# FastSense · Frontend React 18 + Vite + Tailwind
# Build multi-stage: Node compila, Nginx sirve estáticos y hace SPA fallback.
# =====================================================================
# Nota: se usa la variante glibc (slim) y no Alpine/musl porque el parser
# nativo de Rollup falla en este host con "Code is nested too deeply".
FROM node:20-slim AS build

WORKDIR /app

COPY package.json ./
RUN npm install --no-audit --no-fund

COPY . .

ARG VITE_API_BASE_URL=http://localhost:8080
ARG VITE_API_KEY=fs_demo_key_2026
ARG VITE_JWT_TOKEN=
ARG VITE_INTERNAL_TOKEN=fastsense_internal_2026
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL \
    VITE_API_KEY=$VITE_API_KEY \
    VITE_JWT_TOKEN=$VITE_JWT_TOKEN \
    VITE_INTERNAL_TOKEN=$VITE_INTERNAL_TOKEN

RUN npm run build

# ---------------------------------------------------------------------
FROM nginx:alpine AS runner

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
  CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1

CMD ["nginx", "-g", "daemon off;"]

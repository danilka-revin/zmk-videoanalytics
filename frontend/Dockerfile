FROM node:24-alpine AS build
WORKDIR /app
# .npmrc ships resilient fetch-retries/timeouts so `npm ci` below never hangs
# silently against a slow or flaky registry. It is copied before package.json
# so it is present for every install invocation.
COPY .npmrc package.json package-lock.json ./
# Allow overriding the npm registry (e.g. a regional mirror) for installations
# where registry.npmjs.org is unreachable or throttled. Pass via docker-compose
# build arg NPM_REGISTRY (mapped from ZMK_NPM_REGISTRY in .env).
ARG NPM_REGISTRY
RUN if [ -n "$NPM_REGISTRY" ]; then \
      npm config set registry "$NPM_REGISTRY"; \
    fi \
 && npm ci --no-audit --no-fund \
 && npm cache clean --force >/dev/null 2>&1 || true
COPY . .
RUN npm run build

FROM nginx:1.28-alpine
COPY nginx.conf /etc/nginx/templates/default.conf.template
COPY docker-entrypoint.sh /usr/local/bin/zmk-nginx-entrypoint
RUN chmod +x /usr/local/bin/zmk-nginx-entrypoint
RUN sed 's#\${GO2RTC_UPSTREAM}#http://host.docker.internal:1984#' /etc/nginx/templates/default.conf.template > /etc/nginx/conf.d/default.conf
RUN nginx -t
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=15s --timeout=3s --retries=3 CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1
ENTRYPOINT ["/usr/local/bin/zmk-nginx-entrypoint"]

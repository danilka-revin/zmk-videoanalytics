FROM node:24-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
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

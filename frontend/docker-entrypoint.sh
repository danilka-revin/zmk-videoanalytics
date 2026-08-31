#!/bin/sh
# Render nginx.conf at container start so GO2RTC_UPSTREAM can be supplied from
# .env / docker-compose without baking a machine-specific Docker host IP into
# the image. Nginx built-in variables ($api_upstream, $host, $uri, ...) are not
# environment variables and are deliberately left untouched.
set -eu
: "${GO2RTC_UPSTREAM:=http://host.docker.internal:1984}"
envsubst '${GO2RTC_UPSTREAM}' < /etc/nginx/templates/default.conf.template > /etc/nginx/conf.d/default.conf
exec nginx -g 'daemon off;'

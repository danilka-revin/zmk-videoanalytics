#!/bin/sh
# Render nginx.conf at container start so GO2RTC_UPSTREAM can be supplied from
# .env / docker-compose without baking a machine-specific Docker host IP into
# the image. The value is exported so envsubst can see it.
set -eu
GO2RTC_UPSTREAM="${GO2RTC_UPSTREAM:-http://host.docker.internal:1984}"
export GO2RTC_UPSTREAM
envsubst '${GO2RTC_UPSTREAM}' < /etc/nginx/templates/default.conf.template > /etc/nginx/conf.d/default.conf
exec nginx -g 'daemon off;'

#!/bin/sh
set -e
echo "window.MAILFLOW_API_KEY='${MAILFLOW_API_KEY}';" > /usr/share/nginx/html/js/config.js
exec nginx -g 'daemon off;'

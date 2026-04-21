FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY docker-entrypoint.sh /docker-entrypoint.d/40-generate-config-js.sh
RUN chmod +x /docker-entrypoint.d/40-generate-config-js.sh
COPY css/ /usr/share/nginx/html/css/
COPY js/ /usr/share/nginx/html/js/
COPY *.html /usr/share/nginx/html/

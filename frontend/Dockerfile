# Build-Rezept fürs Frontend — Coolify baut damit auf dem Server (kein lokales Docker).
# Non-root-nginx (Security-Audit 040626, #7): läuft als uid 101, lauscht auf 8080 statt 80.
# Coolify-App "Mailflow Frontend": build_pack=dockerfile, base_directory=/frontend, ports_exposes=8080
FROM nginxinc/nginx-unprivileged:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY css/ /usr/share/nginx/html/css/
COPY js/ /usr/share/nginx/html/js/
COPY *.html favicon.svg manifest.json /usr/share/nginx/html/

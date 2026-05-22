FROM nginx:1.27-alpine

COPY ui/nginx.conf /etc/nginx/conf.d/default.conf
COPY ui/index.html /usr/share/nginx/html/index.html
COPY ui/thesis.html /usr/share/nginx/html/thesis.html
COPY ui/docker-entrypoint.sh /docker-entrypoint.d/99-render-grafana-url.sh

RUN chmod +x /docker-entrypoint.d/99-render-grafana-url.sh

FROM python:3.13-alpine
WORKDIR /app
COPY server.py hook.py install.sh ./
COPY hub ./hub
COPY web ./web
RUN apk add --no-cache tzdata && adduser -D -u 10001 hub && mkdir /data /backup && chown hub /data /backup
USER hub
ENV HUB_HOST=0.0.0.0 HUB_PORT=8787 HUB_DB=/data/hub.db PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=3s CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8787/healthz',timeout=2)"
CMD ["python3", "server.py"]

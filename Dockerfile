FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml ./
COPY tracker ./tracker
RUN pip install .

# Site config (positions, calibration) is mounted at /config, never baked in.
RUN useradd --system --uid 1000 tracker && mkdir /config && chown tracker /config
USER tracker

ENV TRACKER_CONFIG=/config/fixtures.json WEB_HOST=0.0.0.0 WEB_PORT=8080
# Art-Net (UDP 6454) and the web UI. For Art-Net discovery to work the container
# must run with host networking (broadcast ArtPoll does not reach a bridge).
EXPOSE 8080 6454/udp
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/api/state' % os.environ['WEB_PORT'], timeout=3)"
CMD ["sh", "-c", "exec python -m tracker --config \"$TRACKER_CONFIG\" --web-host \"$WEB_HOST\" --web-port \"$WEB_PORT\""]

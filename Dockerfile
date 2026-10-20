FROM node:24-bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir anthropic==1.16.0
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts && npm cache clean --force

# Plaud's sign-in opens a browser through the `open` package. There is no browser here, so its
# xdg-open is replaced with one that hands the sign-in link to the web app instead.
COPY docker/capture-url.sh /app/node_modules/open/xdg-open
COPY docker/capture-url.sh /usr/local/bin/xdg-open
RUN chmod 755 /app/node_modules/open/xdg-open /usr/local/bin/xdg-open

COPY plaud_ai.py openplaud_web.py openplaud_notes.py openplaud_ui.py ./

ENV HOME=/data \
    OPENPLAUD_DATA=/data \
    OPENPLAUD_PORT=8795 \
    PYTHONUNBUFFERED=1
RUN mkdir -p /data && chown node:node /data
USER node
VOLUME /data
EXPOSE 8795
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD curl -fsS http://127.0.0.1:8795/healthz || exit 1
CMD ["python3", "/app/openplaud_web.py"]

FROM python:3.12-slim

# All runtime files (config.toml, .env-provided vars, token/service_account
# keys, state.json, the generated feed) live in the /data volume.
WORKDIR /build
COPY pyproject.toml README.md LICENSE requirements.lock ./
COPY src ./src

# requirements.lock is applied as a *constraints* file: pip still resolves only
# what this package actually needs, but every resolved version is pinned to the
# audited set. Without it the production server — which rebuilds itself from
# main every five minutes (scripts/autodeploy.sh) — silently picked up whatever
# each dependency had released that day.
RUN pip install --no-cache-dir -c requirements.lock .

RUN useradd --create-home --uid 1000 ftmo
USER ftmo
WORKDIR /data
VOLUME /data
EXPOSE 8080

# The container runs as uid 1000 against a bind mount. If ./data on the host is
# owned by another uid, nothing persists: run `chown -R 1000:1000 data` once.
# Serve mode checks this at startup and refuses to run rather than looking
# healthy while losing every write (server.check_writable).

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

CMD ["ftmo-calendar", "--config", "/data/config.toml", "serve"]

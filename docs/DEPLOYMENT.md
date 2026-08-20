# Deploying to a VPS with your own domain

This walks through hosting the calendar at a subdomain (the examples use
`calendar.bogdantruta.com`) on any Linux VPS, with DNS on Cloudflare and
automatic HTTPS via Caddy. Total time: ~15 minutes.

## 0. What you need

- A VPS (any small instance works — 1 vCPU / 512 MB is plenty), Ubuntu 22.04+
  assumed below
- Your domain's DNS managed in Cloudflare
- An LLM API key (OpenRouter/Gemini/…)

## 1. Point the subdomain at your VPS (Cloudflare)

In the Cloudflare dashboard → your domain → **DNS** → **Add record**:

| Field | Value |
| --- | --- |
| Type | `A` |
| Name | `calendar` |
| IPv4 address | your VPS's public IP |
| Proxy status | **DNS only** (grey cloud) |

Start with **DNS only** so Caddy can obtain its Let's Encrypt certificate
without surprises. Once everything works you can optionally flip it to
**Proxied** (orange cloud) for Cloudflare's DDoS shielding — if you do, also
set *SSL/TLS → Overview → Full (strict)* in Cloudflare, or you'll get redirect
loops.

DNS propagates in a minute or two; verify with `nslookup calendar.bogdantruta.com`.

## 2. Prepare the VPS

```bash
# Docker + compose plugin
curl -fsSL https://get.docker.com | sh

# Firewall: only SSH and web traffic
sudo ufw allow OpenSSH
sudo ufw allow 80,443/tcp
sudo ufw enable
```

## 3. Install the app

```bash
git clone https://github.com/Bogzx/ftmo-calendar
cd ftmo-calendar
mkdir data
```

`data/config.toml` — feed-only mode (no Google account anywhere):

```toml
[llm]
provider = "openai-compatible"
base_url = "https://openrouter.ai/api/v1"
models = ["deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-pro"]

[calendar]
enabled = false
```

`.env` (next to `compose.yaml`):

```bash
LLM_API_KEY="sk-or-v1-..."
# optional but recommended — get pinged on changes and failures:
# DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
```

The container writes `state.json`, `stats.json` and the feed into `./data` as
uid 1000, so give it that directory:

```bash
sudo chown -R 1000:1000 data
```

(Serve mode checks this at startup and refuses to run if the directory is not
writable — better a loud failure than a container that looks healthy while
every write silently vanishes.)

Start it:

```bash
sudo docker compose up -d
curl -s http://127.0.0.1:8080/healthz   # expect {"ok": true, ...} after ~30s
```

The shipped `compose.yaml` publishes port 8080 on all interfaces, which is what
the README's `http://your-vps:8080/feed.ics` refers to and what you want if the
VPS firewall is your only gate. If 8080 is taken, set `PORT=9000` in `.env` —
no file needs editing.

### Binding to loopback behind a reverse proxy

Once Caddy is the public face (next section), the container no longer needs a
public port at all. Bind it to loopback so nothing but the proxy can reach it:

```yaml
    ports:
      - "127.0.0.1:8133:8080"
```

Pick a host port that no other container on the box is using — this is why the
public instance at `calendar.bogdantruta.com` uses 8133 rather than 8080. Then
point Caddy at the port you chose (section 4) and open only 80/443 in `ufw`.

## 4. HTTPS with Caddy

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

`/etc/caddy/Caddyfile` — the upstream port must be whatever the *host* side of
the `ports:` mapping says. With the loopback binding above (`127.0.0.1:8133`):

```
calendar.bogdantruta.com {
    reverse_proxy 127.0.0.1:8133
}
```

If you kept the shipped default instead, the mapping is `8080:8080`, so proxy
`127.0.0.1:8080`. Proxying a port nothing is bound to is the one mistake here
that produces a working certificate and a 502 on every request.

```bash
sudo systemctl reload caddy
```

Caddy fetches and renews the certificate automatically. That's the whole
HTTPS story.

## 5. Verify

- `https://calendar.bogdantruta.com/` — landing page with the countdown
- `https://calendar.bogdantruta.com/feed.ics` — the feed (subscribe to this)
- `https://calendar.bogdantruta.com/healthz` — `"ok": true`
- `https://calendar.bogdantruta.com/stats` — usage numbers

Subscribe from your own Google Calendar (*Other calendars → + → From URL*) and
share the `/status` page with your group.

## 6. Auto-deploy from GitHub (optional)

Make the server follow `main` automatically: a systemd timer polls GitHub
every 5 minutes and rebuilds only when there are new commits, using the
repo's own `scripts/autodeploy.sh`.

Run this **from inside the clone you actually deployed**. The unit has to name
that directory, which is not necessarily `~/ftmo-calendar` — if you cloned it
under another name, or a second stale clone exists, pointing the timer at the
wrong one is a silent no-op: it fetches, resets and rebuilds a checkout nothing
is running, reporting `Succeeded` every five minutes while the live container
never moves.

```bash
sudo usermod -aG docker $USER   # docker without sudo for the deploy user
cd /path/to/your/clone && APP_DIR="$(pwd)"

sudo tee /etc/systemd/system/ftmo-autodeploy.service >/dev/null <<EOF
[Unit]
Description=Auto-deploy ftmo-calendar from GitHub main
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
User=$USER
Group=docker
ExecStart=$APP_DIR/scripts/autodeploy.sh
EOF

sudo tee /etc/systemd/system/ftmo-autodeploy.timer >/dev/null <<'EOF'
[Unit]
Description=Poll GitHub for ftmo-calendar updates every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
RandomizedDelaySec=30

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ftmo-autodeploy.timer
```

Check it points where you think it does — the deployed directory is whatever
Docker says it is, not whatever matches the repo name:

```bash
systemctl cat ftmo-autodeploy.service | grep ExecStart
docker ps --format '{{.Names}}\t{{.Label "com.docker.compose.project.working_dir"}}'
```

Watch deploys with `journalctl -u ftmo-autodeploy.service -f`. Note this
deploys whatever lands on `main` regardless of CI status — fine for a
single-maintainer repo; use a GitHub-Actions-over-SSH deploy instead if you
want CI-gated deploys.

## 7. Operating it

| Task | Command |
| --- | --- |
| Logs | `sudo docker compose logs -f` |
| Update to a new release | automatic (section 6), or `git pull && sudo docker compose up -d --build` |
| Restart | `sudo docker compose restart` |
| Health from outside | point UptimeRobot (or similar) at `/healthz` |

`/healthz` answers "is this feed trustworthy right now?", not "is the process
up". It returns **503** when the last sync raised, when no successful sync has
landed within twice `sync_interval_minutes`, or when a run completed but
reported an anomaly (the keyword gate matching nothing, or a post's extraction
losing events with none new extracted). A plain HTTP monitor on that URL is
enough — no keyword matching needed. The JSON body carries the detail:

```json
{
  "ok": false,
  "status": "stale",
  "last_success": "2026-07-24T03:00:00+00:00",
  "last_success_age_seconds": 1987200,
  "stale": true,
  "stale_after_seconds": 43200,
  "anomalies": []
}
```

With several firms configured it also returns **503** when any single firm is
unhealthy, with `"status": "degraded"`, the offending firms in
`unhealthy_sources`, and a per-firm breakdown in `sources`:

```json
{
  "ok": false,
  "status": "degraded",
  "unhealthy_sources": ["Topstep"],
  "sources": [
    {"firm": "ftmo", "display_name": "FTMO", "ok": true,
     "status": "ok", "last_success_age_seconds": 1204, "stale": false},
    {"firm": "topstep", "display_name": "Topstep", "ok": false,
     "status": "stale", "last_success_age_seconds": 91500, "stale": true,
     "last_error": "could not fetch https://help.topstep.com/… after 3 attempts"}
  ]
}
```

That is deliberate: a source which has quietly stopped publishing must move the
status code, because averaged into an overall green it stays unnoticed until
someone gets caught by an outage. One firm failing does not stop the others
syncing — the feed keeps updating for every healthy source while the monitor
tells you which one needs attention.

Everything stateful lives in `./data` (`state.json`, `stats.json`, the feed)
and in `.env` — back those up and the deployment is fully reproducible.

The container restarts itself (`restart: unless-stopped`) and has a Docker
healthcheck; a failing sync never takes the feed down, and if you set the
Discord webhook you'll hear about every change and every failure.

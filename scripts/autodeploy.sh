#!/bin/bash
# Poll origin/main and redeploy when it moves — once CI has passed for it. Run
# by a systemd timer: see docs/DEPLOYMENT.md ("Auto-deploy from GitHub").
#
# Environment (all optional; the shipped systemd unit sets none of them):
#   AUTODEPLOY_REQUIRE_CI=0  deploy without waiting for CI (the pre-0.9 behaviour)
#   AUTODEPLOY_REPO          OWNER/REPO to ask about; default: parsed from origin
#   GITHUB_TOKEN             lifts the anonymous GitHub API rate limit
set -euo pipefail

cd "$(dirname "$0")/.."

git fetch -q origin main
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0
fi

if [ "${AUTODEPLOY_REQUIRE_CI:-1}" != "0" ]; then
    # The gate is the copy already deployed, never the one being judged.
    if ! command -v python3 >/dev/null; then
        echo "not deploying ${REMOTE:0:7}: python3 is needed to check CI" \
            "(install it, or set AUTODEPLOY_REQUIRE_CI=0)" >&2
        exit 1
    fi
    REPO="${AUTODEPLOY_REPO:-$(python3 -c 'import sys; sys.path.insert(0, "scripts"); import ci_gate; print(ci_gate.repo_slug(sys.argv[1]) or "")' "$(git remote get-url origin)")}"
    if [ -z "$REPO" ]; then
        echo "not deploying ${REMOTE:0:7}: origin is not a GitHub URL; set AUTODEPLOY_REPO" >&2
        exit 1
    fi
    set +e
    GATE=$(python3 scripts/ci_gate.py "$REPO" "$REMOTE" 2>&1)
    CODE=$?
    set -e
    case "$CODE" in
        0) echo "CI passed for ${REMOTE:0:7}: $GATE" ;;
        10) echo "waiting for CI on ${REMOTE:0:7}: $GATE"; exit 0 ;;
        11) echo "not deploying ${REMOTE:0:7}: $GATE" >&2; exit 1 ;;
        *) echo "not deploying ${REMOTE:0:7}: CI status unknown: $GATE" >&2; exit 1 ;;
    esac
fi

echo "deploying ${REMOTE:0:7} (was ${LOCAL:0:7})"
# reset, not pull: a deploy clone tracks origin/main exactly, even across
# history rewrites or force pushes
git reset --hard -q origin/main
docker compose up -d --build
echo "deployed ${REMOTE:0:7}"

#!/usr/bin/env bash
#
# Ship new code to an already-provisioned box.
#
#   sudo bash /opt/fleet/deploy/update.sh
#
# Does not touch credentials, the broker, swap or packages -- provision.sh owns those.
# Re-running provision.sh instead would also work and is safe, just several minutes
# slower because it reinstalls apt packages and rebuilds the venv from scratch.

set -euo pipefail

SERVICE_USER="fleet"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

if [[ "${EUID}" -ne 0 ]]; then
    echo "update.sh must run as root: sudo bash ${BASH_SOURCE[0]}" >&2
    exit 1
fi

say "Fetching ${APP_DIR}"
# The checkout is owned by the fleet user but git is run as root; without this git 2.35+
# refuses to operate on a tree it considers someone else's.
git config --global --add safe.directory "${APP_DIR}" 2>/dev/null || true
git -C "${APP_DIR}" fetch --quiet origin
BEFORE="$(git -C "${APP_DIR}" rev-parse --short HEAD)"
git -C "${APP_DIR}" reset --hard --quiet "origin/$(git -C "${APP_DIR}" rev-parse --abbrev-ref HEAD)"
AFTER="$(git -C "${APP_DIR}" rev-parse --short HEAD)"
echo "${BEFORE} -> ${AFTER}"

say "Updating dependencies"
"${APP_DIR}/.venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"

say "Rebuilding the dashboard"
pushd "${APP_DIR}/dashboard" >/dev/null
npm ci --no-audit --no-fund
npm run build
popd >/dev/null

test -f "${APP_DIR}/dashboard/dist/index.html" || {
    echo "dashboard build produced no dist/index.html; not restarting" >&2
    exit 1
}

# The unit files are part of the checkout, so a change to them ships like any other
# change. Rewritten for APP_DIR the same way provision.sh does it.
say "Reinstalling systemd units"
for unit in fleet-backend fleet-simulator; do
    sed "s|/opt/fleet|${APP_DIR}|g" "${APP_DIR}/deploy/${unit}.service" \
        > "/etc/systemd/system/${unit}.service"
done
systemctl daemon-reload

chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"

say "Restarting services"
systemctl restart fleet-backend fleet-simulator

for _ in $(seq 1 30); do
    if curl -fsS --max-time 2 http://127.0.0.1/healthz >/dev/null 2>&1; then
        printf '\n\033[1;32mUpdated to %s and healthy.\033[0m\n\n' "${AFTER}"
        exit 0
    fi
    sleep 1
done

echo "backend did not come back healthy; journalctl -u fleet-backend -n 50" >&2
systemctl --no-pager --lines=20 status fleet-backend || true
exit 1

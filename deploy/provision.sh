#!/usr/bin/env bash
#
# One-time provisioning for a fresh Ubuntu 24.04 t3.micro.
#
#   sudo apt-get update && sudo apt-get install -y git
#   sudo git clone https://github.com/Nazgullss/Peppermint.git /opt/fleet
#   sudo bash /opt/fleet/deploy/provision.sh
#
# Safe to re-run: it reuses the credentials it generated the first time, so re-running
# never locks the services out of the broker. To ship new code afterwards use
# deploy/update.sh, which is much faster because it skips all of this.

set -euo pipefail

MQTT_USER="fleet"
SERVICE_USER="fleet"
ENV_DIR="/etc/fleet"
ENV_FILE="${ENV_DIR}/fleet.env"
SWAP_FILE="/swapfile"
SWAP_SIZE_MB=2048

# Resolve the checkout from this script's own location, so the box is not required to
# have cloned to any particular path -- the unit files are rewritten to match below.
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

if [[ "${EUID}" -ne 0 ]]; then
    echo "provision.sh must run as root: sudo bash ${BASH_SOURCE[0]}" >&2
    exit 1
fi

say "Provisioning from ${APP_DIR}"

# ======================================================================================
# Swap
# ======================================================================================
# A t3.micro has 1 GB and no swap. `npm ci` plus `tsc -b && vite build` is the peak
# memory moment of the whole deploy and will be OOM-killed without this. It also keeps a
# runaway fleet_size from taking the box down instead of just the simulator.

if ! swapon --show --noheadings | grep -q .; then
    say "Creating ${SWAP_SIZE_MB} MB swap"
    fallocate -l "${SWAP_SIZE_MB}M" "${SWAP_FILE}" || \
        dd if=/dev/zero of="${SWAP_FILE}" bs=1M count="${SWAP_SIZE_MB}" status=none
    chmod 600 "${SWAP_FILE}"
    mkswap "${SWAP_FILE}" >/dev/null
    swapon "${SWAP_FILE}"
    grep -q "^${SWAP_FILE}" /etc/fstab || echo "${SWAP_FILE} none swap sw 0 0" >> /etc/fstab
    # Swap is an insurance policy, not a tier of memory. Only reach for it under real
    # pressure, so the load-test numbers describe RAM behaviour and not paging.
    sysctl -qw vm.swappiness=10
    grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf
else
    say "Swap already present, skipping"
fi

# ======================================================================================
# Packages
# ======================================================================================

say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# Ubuntu 24.04 ships Python 3.12. simulator/main.py uses asyncio.TaskGroup and `except*`,
# so anything older than 3.11 will not start.
apt-get install -y -qq \
    python3 python3-venv python3-pip \
    mosquitto mosquitto-clients \
    git curl ca-certificates

if ! command -v node >/dev/null 2>&1; then
    # Node only exists on this box to build the dashboard. 24.04's packaged Node is 18,
    # which is EOL; vite 6 and tsc 5.7 are happier on 22.
    say "Installing Node.js 22"
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
    apt-get install -y -qq nodejs
fi

python3 --version
node --version

# ======================================================================================
# Service account
# ======================================================================================

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    say "Creating service user '${SERVICE_USER}'"
    # No login shell and no home: this account exists to own two processes.
    useradd --system --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

# ======================================================================================
# Credentials
# ======================================================================================
# Generated once and reused on every later run. Regenerating the MQTT password without
# also rewriting /etc/mosquitto/passwd would leave both services unable to connect, and
# a rotated admin token would silently invalidate whatever the operator had noted down.

mkdir -p "${ENV_DIR}"

if [[ -f "${ENV_FILE}" ]]; then
    say "Reusing existing credentials from ${ENV_FILE}"
    MQTT_PASSWORD="$(sed -n 's/^SIM_MQTT_PASSWORD=//p' "${ENV_FILE}" | head -1)"
    ADMIN_TOKEN="$(sed -n 's/^BACKEND_ADMIN_TOKEN=//p' "${ENV_FILE}" | head -1)"
fi

# tr -d '/+=' keeps these safe to paste into a shell, a URL and a systemd
# EnvironmentFile without quoting rules differing between the three.
: "${MQTT_PASSWORD:=$(openssl rand -base64 32 | tr -d '/+=' | cut -c1-32)}"
: "${ADMIN_TOKEN:=$(openssl rand -base64 48 | tr -d '/+=' | cut -c1-43)}"

if [[ -z "${MQTT_PASSWORD}" || -z "${ADMIN_TOKEN}" ]]; then
    echo "failed to obtain credentials" >&2
    exit 1
fi

# ======================================================================================
# Broker
# ======================================================================================

say "Configuring mosquitto"
mosquitto_passwd -c -b /etc/mosquitto/passwd "${MQTT_USER}" "${MQTT_PASSWORD}"
chown root:mosquitto /etc/mosquitto/passwd
chmod 0640 /etc/mosquitto/passwd

install -m 0644 "${APP_DIR}/deploy/mosquitto.prod.conf" /etc/mosquitto/conf.d/fleet.conf
systemctl enable mosquitto >/dev/null
systemctl restart mosquitto

# Fail loudly here rather than letting both services retry a broker that will never
# accept them. A wrong password looks exactly like a network problem in the logs.
if ! mosquitto_pub -h 127.0.0.1 -p 1883 \
        -u "${MQTT_USER}" -P "${MQTT_PASSWORD}" \
        -t "fleet/_provision/check" -m ok -q 0; then
    echo "broker rejected the generated credentials; check journalctl -u mosquitto" >&2
    exit 1
fi
echo "broker accepts authenticated publishes on 127.0.0.1:1883"

# ======================================================================================
# Application
# ======================================================================================

say "Building the Python environment"
python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/pip" install --quiet --upgrade pip
"${APP_DIR}/.venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"

say "Building the dashboard"
# dashboard/dist is gitignored, so a fresh clone has no frontend at all. The backend
# mounts dist at / only if the directory exists, which means skipping this step yields a
# working API and a 404 at the root -- a confusing way to find out.
pushd "${APP_DIR}/dashboard" >/dev/null
npm ci --no-audit --no-fund
npm run build
popd >/dev/null

test -f "${APP_DIR}/dashboard/dist/index.html" || {
    echo "dashboard build produced no dist/index.html" >&2
    exit 1
}

# ======================================================================================
# Environment file
# ======================================================================================

say "Writing ${ENV_FILE}"
sed -e "s|^SIM_MQTT_PASSWORD=.*|SIM_MQTT_PASSWORD=${MQTT_PASSWORD}|" \
    -e "s|^BACKEND_MQTT_PASSWORD=.*|BACKEND_MQTT_PASSWORD=${MQTT_PASSWORD}|" \
    -e "s|^BACKEND_ADMIN_TOKEN=.*|BACKEND_ADMIN_TOKEN=${ADMIN_TOKEN}|" \
    "${APP_DIR}/deploy/fleet.env.example" > "${ENV_FILE}"
chown root:"${SERVICE_USER}" "${ENV_FILE}"
chmod 0640 "${ENV_FILE}"

# The services only ever read the checkout; ProtectSystem=strict makes that structural.
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"

# ======================================================================================
# systemd
# ======================================================================================

say "Installing systemd units"
for unit in fleet-backend fleet-simulator; do
    sed "s|/opt/fleet|${APP_DIR}|g" "${APP_DIR}/deploy/${unit}.service" \
        > "/etc/systemd/system/${unit}.service"
done
systemctl daemon-reload
systemctl enable fleet-backend fleet-simulator >/dev/null
systemctl restart fleet-backend fleet-simulator

# ======================================================================================
# Verify
# ======================================================================================

say "Waiting for the backend to answer"
for _ in $(seq 1 30); do
    if curl -fsS --max-time 2 http://127.0.0.1/healthz >/dev/null 2>&1; then
        healthy=1
        break
    fi
    sleep 1
done

if [[ "${healthy:-0}" -ne 1 ]]; then
    echo "backend did not become healthy; journalctl -u fleet-backend -n 50" >&2
    systemctl --no-pager --lines=20 status fleet-backend || true
    exit 1
fi

TOKEN="$(curl -sS -X PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 60" --max-time 2 2>/dev/null || true)"
PUBLIC_IP="$(curl -sS -H "X-aws-ec2-metadata-token: ${TOKEN}" \
    --max-time 2 http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || true)"
: "${PUBLIC_IP:=<this-instance-public-ip>}"

cat <<EOF

$(printf '\033[1;32m')Deployed.$(printf '\033[0m')

  Dashboard   http://${PUBLIC_IP}/
  Stats       http://${PUBLIC_IP}/api/stats
  Health      http://${PUBLIC_IP}/healthz

  Admin token ${ADMIN_TOKEN}

  Turn the three knobs on the running box, no restart:

    curl -X POST http://${PUBLIC_IP}/api/admin/config \\
      -H "Authorization: Bearer ${ADMIN_TOKEN}" \\
      -H 'Content-Type: application/json' \\
      -d '{"fleet_size":500,"update_interval_ms":250,"publish_batch_size":100}'

  If the dashboard does not load from your laptop, the inbound rule is missing:
  the instance security group needs TCP 80 open. Port 1883 must stay closed --
  the broker is bound to 127.0.0.1 and is not meant to be reachable.

  Logs   journalctl -u fleet-backend -f
         journalctl -u fleet-simulator -f
  Ship   sudo bash ${APP_DIR}/deploy/update.sh

EOF

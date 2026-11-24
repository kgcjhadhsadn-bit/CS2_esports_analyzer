#!/usr/bin/env bash
# One-time (and re-runnable) preparation of a fresh Ubuntu 24.04 / 22.04 VPS for cs2bot.
#
#   sudo bash deploy/linux/provision.sh [--domain bot.example.pl] [--repo URL] [--branch main]
#                                       [--dir /opt/cs2bot] [--user cs2bot] [--ssh-port 22]
#                                       [--no-swap] [--keep-password-login] [--no-auto-reboot] [--yes]
#
# What it does (every step is idempotent - running it again only fixes what is missing):
#   1. system updates; unattended security upgrades (automatic reboot at 04:45 Warsaw time if needed)
#   2. time zone Europe/Warsaw + NTP time sync (pre-match bets and TLS need an exact clock)
#   3. firewall (ufw): only SSH, 80 and 443 in; fail2ban for SSH; SSH key-only login when a key exists
#   4. a swap file when the VPS has less than 8 GB RAM
#   5. Docker Engine + the compose plugin from Docker's official repository
#   6. the "cs2bot" user (member of the docker group) and the repository in /opt/cs2bot
#   7. .env from .env.example with the VPS values and freshly generated secrets (chmod 600); an
#      existing .env is never overwritten - only missing secrets / keys are added
#   8. systemd timers: nightly database backup, weekly Docker clean-up; logrotate for logs/
#
# After it: edit /opt/cs2bot/.env (docs/VPS_SETUP.md, section 5), then run
#   sudo -u cs2bot /opt/cs2bot/deploy/linux/deploy.sh
set -euo pipefail

DOMAIN=""
REPO_URL="${CS2BOT_REPO_URL:-}"
BRANCH="${CS2BOT_BRANCH:-main}"
TARGET_DIR="/opt/cs2bot"
APP_USER="cs2bot"
SSH_PORT=""
MAKE_SWAP=1
KEEP_PASSWORD_LOGIN=0
AUTO_REBOOT=1
ASSUME_YES=0
SWAP_SIZE_GB=4
DESIRED_UID=10001

usage() {
    sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while (($#)); do
    case "$1" in
        --domain) DOMAIN="${2:?--domain needs a value}"; shift 2 ;;
        --repo) REPO_URL="${2:?--repo needs a value}"; shift 2 ;;
        --branch) BRANCH="${2:?--branch needs a value}"; shift 2 ;;
        --dir) TARGET_DIR="${2:?--dir needs a value}"; shift 2 ;;
        --user) APP_USER="${2:?--user needs a value}"; shift 2 ;;
        --ssh-port) SSH_PORT="${2:?--ssh-port needs a value}"; shift 2 ;;
        --no-swap) MAKE_SWAP=0; shift ;;
        --keep-password-login) KEEP_PASSWORD_LOGIN=1; shift ;;
        --no-auto-reboot) AUTO_REBOOT=0; shift ;;
        -y | --yes) ASSUME_YES=1; shift ;;
        -h | --help) usage 0 ;;
        *) echo "unknown option: $1" >&2; usage 1 ;;
    esac
done
export ASSUME_YES

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

log() { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[warn]\033[0m %s\n' "$*" >&2; }
die() {
    printf '\033[31m[error]\033[0m %s\n' "$*" >&2
    exit 1
}

[[ "$(id -u)" -eq 0 ]] || die "run as root: sudo bash $0"
[[ -r /etc/os-release ]] || die "/etc/os-release missing: this script supports Ubuntu 22.04 / 24.04"
# shellcheck disable=SC1091
. /etc/os-release
if [[ "${ID:-}" != "ubuntu" ]]; then
    warn "this script is written for Ubuntu (found ${PRETTY_NAME:-unknown}); continuing anyway"
elif [[ "${VERSION_ID:-}" != "24.04" && "${VERSION_ID:-}" != "22.04" ]]; then
    warn "tested for Ubuntu 24.04 / 22.04 (found $VERSION_ID); continuing anyway"
fi

export DEBIAN_FRONTEND=noninteractive
APT_OPTS=(-y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)

# ----------------------------------------------------------------------------------- 1. packages
log "1/8 System updates and base packages"
apt-get update -q
apt-get "${APT_OPTS[@]}" upgrade
apt-get "${APT_OPTS[@]}" install --no-install-recommends \
    ca-certificates curl git gnupg openssl ufw fail2ban python3-systemd unattended-upgrades \
    logrotate rclone util-linux iproute2

cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
{
    echo '// cs2bot (deploy/linux/provision.sh): security updates install by themselves.'
    if ((AUTO_REBOOT)); then
        echo '// A kernel update needs a reboot; Docker brings every service back afterwards.'
        echo 'Unattended-Upgrade::Automatic-Reboot "true";'
        echo 'Unattended-Upgrade::Automatic-Reboot-Time "04:45";'
    else
        echo 'Unattended-Upgrade::Automatic-Reboot "false";'
    fi
    echo 'Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";'
    echo 'Unattended-Upgrade::Remove-Unused-Dependencies "true";'
} >/etc/apt/apt.conf.d/52cs2bot-unattended-upgrades
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true

# --------------------------------------------------------------------------------------- 2. time
log "2/8 Time zone Europe/Warsaw and NTP"
timedatectl set-timezone Europe/Warsaw || warn "could not set the time zone"
if ! timedatectl set-ntp true 2>/dev/null; then
    apt-get "${APT_OPTS[@]}" install --no-install-recommends systemd-timesyncd || true
    timedatectl set-ntp true || warn "could not enable NTP: install chrony (apt install chrony)"
fi

# ---------------------------------------------------------------------------- 3. firewall + SSH
log "3/8 Firewall (ufw), fail2ban, SSH"
detected_ssh_ports=()
if [[ -n "$SSH_PORT" ]]; then
    detected_ssh_ports+=("$SSH_PORT")
fi
# The port of the SSH session running this script, and the ports sshd listens on: never lock us out.
if [[ -n "${SSH_CONNECTION:-}" ]]; then
    detected_ssh_ports+=("$(awk '{print $4}' <<<"$SSH_CONNECTION")")
fi
if command -v sshd >/dev/null 2>&1; then
    while read -r port; do
        [[ -n "$port" ]] && detected_ssh_ports+=("$port")
    done < <(sshd -T 2>/dev/null | awk '$1 == "port" {print $2}')
fi
((${#detected_ssh_ports[@]})) || detected_ssh_ports=(22)
mapfile -t ssh_ports < <(printf '%s\n' "${detected_ssh_ports[@]}" | grep -E '^[0-9]+$' | sort -un)

ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
for port in "${ssh_ports[@]}"; do
    ufw limit "${port}/tcp" comment 'SSH' >/dev/null
done
ufw allow 80/tcp comment 'HTTP (Caddy, certificates)' >/dev/null
ufw allow 443/tcp comment 'HTTPS dashboard (Caddy)' >/dev/null
ufw --force enable >/dev/null
log "ufw: allowed SSH (${ssh_ports[*]}), 80, 443. Note: Docker's published ports bypass ufw - only Caddy's 80/443 are published."

mkdir -p /etc/fail2ban/jail.d
cat >/etc/fail2ban/jail.d/cs2bot.local <<EOF
# cs2bot (deploy/linux/provision.sh): ban IPs that keep failing SSH logins.
[sshd]
enabled = true
backend = systemd
port = $(IFS=,; echo "${ssh_ports[*]}")
maxretry = 5
findtime = 10m
bantime = 1h
EOF
systemctl enable fail2ban >/dev/null 2>&1 || true
systemctl restart fail2ban || warn "fail2ban did not start: check 'journalctl -u fail2ban'"

key_found=0
for keyfile in /root/.ssh/authorized_keys ${SUDO_USER:+/home/$SUDO_USER/.ssh/authorized_keys}; do
    if [[ -s "$keyfile" ]] && grep -qE '^(ssh-|ecdsa-|sk-)' "$keyfile"; then
        key_found=1
    fi
done
if ((KEEP_PASSWORD_LOGIN)); then
    log "SSH password login left as it is (--keep-password-login)"
elif ((key_found)); then
    cat >/etc/ssh/sshd_config.d/50-cs2bot.conf <<'EOF'
# cs2bot (deploy/linux/provision.sh): SSH keys only. Remove this file to allow passwords again.
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
    if sshd -t 2>/dev/null; then
        systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
        log "SSH: password login disabled (keys only)"
    else
        rm -f /etc/ssh/sshd_config.d/50-cs2bot.conf
        warn "sshd rejected the hardened config; left SSH unchanged"
    fi
else
    warn "no SSH key in authorized_keys: password login stays ON. Set up a key (docs/VPS_SETUP.md, section 2) and run this script again."
fi

# --------------------------------------------------------------------------------------- 4. swap
log "4/8 Memory and swap"
mem_kb="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
mem_gb=$(((mem_kb + 524288) / 1048576))
if ((MAKE_SWAP)) && ((mem_kb < 8 * 1024 * 1024)); then
    if swapon --show=NAME --noheadings | grep -q .; then
        log "swap already active ($(swapon --show=NAME,SIZE --noheadings | tr '\n' ' '))"
    else
        if [[ ! -f /swapfile ]]; then
            fallocate -l "${SWAP_SIZE_GB}G" /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=$((SWAP_SIZE_GB * 1024))
            chmod 600 /swapfile
            mkswap /swapfile >/dev/null
        fi
        swapon /swapfile
        grep -qE '^/swapfile\s' /etc/fstab || echo '/swapfile none swap sw 0 0' >>/etc/fstab
        log "created a ${SWAP_SIZE_GB} GB swap file (RAM: ~${mem_gb} GB)"
    fi
    printf 'vm.swappiness=10\n' >/etc/sysctl.d/99-cs2bot.conf
    sysctl -q -p /etc/sysctl.d/99-cs2bot.conf || true
else
    log "RAM ~${mem_gb} GB: no swap file needed"
fi

# ------------------------------------------------------------------------------------- 5. docker
log "5/8 Docker Engine + compose plugin"
if docker compose version >/dev/null 2>&1; then
    log "Docker already installed: $(docker --version)"
else
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    arch="$(dpkg --print-architecture)"
    codename="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
    [[ -n "$codename" ]] || die "cannot determine the Ubuntu release codename"
    echo "deb [arch=${arch} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${codename} stable" \
        >/etc/apt/sources.list.d/docker.list
    apt-get update -q
    apt-get "${APT_OPTS[@]}" install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
if [[ ! -f /etc/docker/daemon.json ]]; then
    mkdir -p /etc/docker
    # Default log rotation for every container, and containers keep running while dockerd restarts.
    cat >/etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "20m", "max-file": "5" },
  "live-restore": true
}
EOF
    systemctl restart docker
fi
systemctl enable --now docker >/dev/null 2>&1
docker compose version >/dev/null || die "docker compose is not working"

# --------------------------------------------------------------------------- 6. user + checkout
log "6/8 User '$APP_USER' and repository in $TARGET_DIR"
if ! id "$APP_USER" >/dev/null 2>&1; then
    uid_opt=()
    if ! getent passwd "$DESIRED_UID" >/dev/null && ! getent group "$DESIRED_UID" >/dev/null; then
        groupadd --gid "$DESIRED_UID" "$APP_USER"
        uid_opt=(--uid "$DESIRED_UID" --gid "$DESIRED_UID")
    fi
    useradd --create-home --shell /bin/bash "${uid_opt[@]}" "$APP_USER"
    log "created user $APP_USER (uid $(id -u "$APP_USER"))"
fi
usermod -aG docker "$APP_USER"
APP_UID="$(id -u "$APP_USER")"
APP_GID="$(id -g "$APP_USER")"
APP_HOME="$(getent passwd "$APP_USER" | cut -d: -f6)"

# The owner's SSH keys also work for the cs2bot user (ssh cs2bot@<vps>).
install -d -m 700 -o "$APP_USER" -g "$APP_GID" "$APP_HOME/.ssh"
for keyfile in /root/.ssh/authorized_keys ${SUDO_USER:+/home/$SUDO_USER/.ssh/authorized_keys}; do
    if [[ -s "$keyfile" ]]; then
        touch "$APP_HOME/.ssh/authorized_keys"
        while IFS= read -r key; do
            [[ -n "$key" && "$key" != \#* ]] || continue
            grep -qxF "$key" "$APP_HOME/.ssh/authorized_keys" || printf '%s\n' "$key" >>"$APP_HOME/.ssh/authorized_keys"
        done <"$keyfile"
    fi
done
[[ -f "$APP_HOME/.ssh/authorized_keys" ]] && chown "$APP_USER:$APP_GID" "$APP_HOME/.ssh/authorized_keys" &&
    chmod 600 "$APP_HOME/.ssh/authorized_keys"
# A key for pulling a private repository (add the .pub as a read-only "deploy key" on GitHub).
if [[ ! -f "$APP_HOME/.ssh/id_ed25519" ]]; then
    sudo -u "$APP_USER" -H ssh-keygen -q -t ed25519 -N '' -C "cs2bot@$(hostname)" -f "$APP_HOME/.ssh/id_ed25519"
fi

as_app() { sudo -u "$APP_USER" -H "$@"; }

if [[ -d "$TARGET_DIR/.git" ]]; then
    chown -R "$APP_USER:$APP_GID" "$TARGET_DIR"
    if [[ "$SOURCE_ROOT" != "$(cd "$TARGET_DIR" && pwd)" ]]; then
        if as_app git -C "$TARGET_DIR" pull --ff-only; then
            log "repository updated"
        else
            warn "git pull failed in $TARGET_DIR (no access to the remote?) - continuing with the current checkout"
        fi
    fi
elif [[ -n "$REPO_URL" ]]; then
    mkdir -p "$TARGET_DIR"
    chown "$APP_USER:$APP_GID" "$TARGET_DIR"
    if ! as_app git clone --branch "$BRANCH" "$REPO_URL" "$TARGET_DIR"; then
        echo
        echo "Cloning $REPO_URL failed. For a private repository add this key as a read-only deploy key"
        echo "(GitHub: repository -> Settings -> Deploy keys) and use the SSH URL (git@github.com:...):"
        cat "$APP_HOME/.ssh/id_ed25519.pub"
        exit 1
    fi
elif [[ -d "$SOURCE_ROOT/.git" ]]; then
    # Run from a checkout elsewhere (e.g. cloned into /root): copy it, keep its origin for updates.
    git clone --quiet --no-hardlinks "$SOURCE_ROOT" "$TARGET_DIR"
    origin="$(git -C "$SOURCE_ROOT" remote get-url origin 2>/dev/null || true)"
    if [[ -n "$origin" ]]; then
        git -C "$TARGET_DIR" remote set-url origin "$origin"
    fi
    chown -R "$APP_USER:$APP_GID" "$TARGET_DIR"
    log "copied the checkout $SOURCE_ROOT to $TARGET_DIR (origin: ${origin:-none})"
else
    die "no repository: pass --repo <git URL> (or run this script from a checkout)"
fi

# Runtime directories owned by the container user (same uid as $APP_USER).
install -d -m 750 -o "$APP_USER" -g "$APP_GID" "$TARGET_DIR/data" "$TARGET_DIR/logs"
install -d -m 700 -o "$APP_USER" -g "$APP_GID" "$TARGET_DIR/backups"

# ---------------------------------------------------------------------------------------- 7. .env
log "7/8 Configuration file .env"
ENV_PATH="$TARGET_DIR/.env"
# shellcheck source=deploy/linux/common.sh
. "$TARGET_DIR/deploy/linux/common.sh"

gen_hex() { openssl rand -hex "$1"; }
# Letters and digits only: no character that needs quoting in .env, a URL or a shell.
gen_password() { openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c "$1"; }

public_host="$DOMAIN"
if [[ -z "$public_host" ]]; then
    public_host="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}' | head -n 1)"
fi
is_ip() { [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ || "$1" == *:*:* ]]; }
if [[ -z "$DOMAIN" && "$public_host" =~ ^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.) ]]; then
    warn "this VPS's own address $public_host is private (NAT): put the public IP or a domain into CS2BOT_PUBLIC_HOST in .env"
fi

fresh=0
if [[ ! -f "$ENV_PATH" ]]; then
    [[ -f "$TARGET_DIR/.env.example" ]] || die "$TARGET_DIR/.env.example is missing"
    install -m 600 -o "$APP_USER" -g "$APP_GID" "$TARGET_DIR/.env.example" "$ENV_PATH"
    printf '\n# --- VPS settings (deploy/linux/provision.sh, docs/VPS_SETUP.md) --------------------\n' >>"$ENV_PATH"
    fresh=1
fi
chown "$APP_USER:$APP_GID" "$ENV_PATH"
chmod 600 "$ENV_PATH"

# set_default KEY VALUE: on a fresh .env always; on an existing one only when KEY is missing/empty.
set_default() {
    local key="$1" value="$2"
    if ((fresh)) || [[ -z "$(env_get "$key" "$ENV_PATH")" ]]; then
        env_set "$key" "$value" "$ENV_PATH"
    fi
}

set_default COMPOSE_FILE docker-compose.cloud.yml
set_default APP_UID "$APP_UID"
set_default APP_GID "$APP_GID"
set_default CS2BOT_ENVIRONMENT prod
set_default CS2BOT_LOG_JSON true
set_default POSTGRES_USER cs2bot
set_default POSTGRES_DB cs2bot
[[ -n "$(env_get POSTGRES_PASSWORD "$ENV_PATH")" ]] || env_set POSTGRES_PASSWORD "$(gen_hex 24)" "$ENV_PATH"
pg_user="$(env_get POSTGRES_USER "$ENV_PATH")"
pg_db="$(env_get POSTGRES_DB "$ENV_PATH")"
pg_password="$(env_get POSTGRES_PASSWORD "$ENV_PATH")"
# Informational on the VPS: docker-compose.cloud.yml builds the app's URL from POSTGRES_* itself.
env_set CS2BOT_DATABASE__URL "postgresql+psycopg://${pg_user:-cs2bot}:${pg_password}@postgres:5432/${pg_db:-cs2bot}" "$ENV_PATH"
# The API listens on the container network (Caddy in front): password + session secret required.
set_default CS2BOT_API__HOST 0.0.0.0
[[ -n "$(env_get CS2BOT_API__PASSWORD "$ENV_PATH")" ]] || env_set CS2BOT_API__PASSWORD "$(gen_password 24)" "$ENV_PATH"
[[ -n "$(env_get CS2BOT_API__SESSION_SECRET "$ENV_PATH")" ]] || env_set CS2BOT_API__SESSION_SECRET "$(gen_hex 32)" "$ENV_PATH"
if [[ -n "$DOMAIN" ]]; then
    env_set CS2BOT_PUBLIC_HOST "$DOMAIN" "$ENV_PATH"
    env_set CS2BOT_TLS_MODE auto "$ENV_PATH"
elif [[ -n "$public_host" ]]; then
    if [[ -z "$(env_get CS2BOT_PUBLIC_HOST "$ENV_PATH")" ]]; then
        env_set CS2BOT_PUBLIC_HOST "$public_host" "$ENV_PATH"
        if is_ip "$public_host"; then
            env_set CS2BOT_TLS_MODE internal "$ENV_PATH"
        else
            env_set CS2BOT_TLS_MODE auto "$ENV_PATH"
        fi
    fi
fi
# Unattended operation (PLAN.md 0b); real money only after the dry-run smoke test (VPS_SETUP.md).
set_default CS2BOT_EXECUTOR__DRY_RUN true
set_default CS2BOT_EXECUTOR__HEADLESS false
set_default CS2BOT_AUTONOMY__ENABLED true
set_default CS2BOT_AUTONOMY__AUTO_LOGIN true
set_default CS2BOT_BETCLIC__CREDENTIALS_SOURCE env
set_default CS2BOT_TELEGRAM__ENABLED true
chown "$APP_USER:$APP_GID" "$ENV_PATH"
chmod 600 "$ENV_PATH"
if ((fresh)); then
    log ".env created with generated secrets (chmod 600). Fill in the remaining values (see below)."
else
    log ".env existed: kept your values, added only what was missing"
fi

# -------------------------------------------------------------------------- 8. timers, logrotate
log "8/8 systemd timers (backup, Docker clean-up) and logrotate"
for unit in "$TARGET_DIR"/deploy/linux/systemd/*.service "$TARGET_DIR"/deploy/linux/systemd/*.timer; do
    [[ -f "$unit" ]] || continue
    sed -e "s|@DIR@|$TARGET_DIR|g" -e "s|@USER@|$APP_USER|g" "$unit" >"/etc/systemd/system/$(basename "$unit")"
done
systemctl daemon-reload
systemctl enable --now cs2bot-backup.timer cs2bot-prune.timer >/dev/null
sed -e "s|@DIR@|$TARGET_DIR|g" -e "s|@USER@|$APP_USER|g" "$TARGET_DIR/deploy/linux/logrotate.conf" \
    >/etc/logrotate.d/cs2bot
chmod 644 /etc/logrotate.d/cs2bot

cat <<EOF

$(printf '\033[32m')Provisioning finished.$(printf '\033[0m')

Next steps (docs/VPS_SETUP.md explains each one):
  1. Fill in the secrets:   sudo -u $APP_USER nano $ENV_PATH
       CS2BOT_PANDASCORE__API_TOKEN (optional), ANTHROPIC_API_KEY,
       CS2BOT_BETCLIC__USERNAME / CS2BOT_BETCLIC__PASSWORD, CS2BOT_BETCLIC__PROXY_URL (if needed),
       CS2BOT_TELEGRAM__BOT_TOKEN / CS2BOT_TELEGRAM__CHAT_ID, bankroll and limits.
       Keep CS2BOT_EXECUTOR__DRY_RUN=true until the smoke test passed.
  2. Start it:              sudo -u $APP_USER $TARGET_DIR/deploy/linux/deploy.sh
  3. Dashboard:             https://$(env_get CS2BOT_PUBLIC_HOST "$ENV_PATH")   user: $(env_get CS2BOT_API__USERNAME "$ENV_PATH" || true)
                            password: sudo grep CS2BOT_API__PASSWORD $ENV_PATH

Deploy key of the $APP_USER user (only needed for 'git pull' of a private repository):
  $(cat "$APP_HOME/.ssh/id_ed25519.pub")
EOF

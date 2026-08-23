#!/bin/bash
set -e

INSTALL_DIR="/opt/rsyncgui"
PORT=3326
SERVICE_NAME="rsyncgui"
REPO_URL="https://github.com/hirogura/rsyncgui.git"
BRANCH="main"

echo "=== rsyncGUI Installer (GitHub版) ==="
echo "Source: ${REPO_URL}"

# ── Tailscale 検出（あれば Tailscale Serve で HTTPS 化する）──
TS_IP=""
TS_DOMAIN=""
if command -v tailscale >/dev/null 2>&1 && tailscale status >/dev/null 2>&1; then
    TS_IP=$(tailscale status --json 2>/dev/null | python3 -c "import json,sys; print((json.load(sys.stdin).get('Self', {}).get('TailscaleIPs') or [''])[0])" 2>/dev/null || true)
    TS_DOMAIN=$(tailscale status --json 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('Self', {}).get('DNSName', '').rstrip('.'))" 2>/dev/null || true)
fi
TS_HTTPS=0
if [ -n "${TS_IP}" ]; then TS_HTTPS=1; fi

# Detect package manager
if command -v apt-get &>/dev/null; then
    PKG="apt"
elif command -v yum &>/dev/null; then
    PKG="yum"
elif command -v dnf &>/dev/null; then
    PKG="dnf"
elif command -v apk &>/dev/null; then
    PKG="apk"
else
    echo "Unsupported package manager. Install python3, rsync, sshpass, git manually."
    exit 1
fi

echo "[1/6] Checking dependencies..."
NEED_PKGS=()
command -v python3 &>/dev/null || NEED_PKGS+=("python3")
command -v rsync &>/dev/null || NEED_PKGS+=("rsync")
command -v sshpass &>/dev/null || NEED_PKGS+=("sshpass")
command -v ssh &>/dev/null || NEED_PKGS+=("__ssh_client__")
command -v git &>/dev/null || NEED_PKGS+=("git")

if [ "${#NEED_PKGS[@]}" -eq 0 ]; then
    echo "  -> python3, rsync, sshpass, ssh, git are already installed. Skipping."
else
    echo "  -> Installing missing packages: ${NEED_PKGS[*]}"
    case $PKG in
        apt)
            PKGS_TO_INSTALL=()
            for p in "${NEED_PKGS[@]}"; do
                if [ "$p" = "__ssh_client__" ]; then
                    PKGS_TO_INSTALL+=("openssh-client")
                else
                    PKGS_TO_INSTALL+=("$p")
                fi
            done
            apt-get update -qq
            apt-get install -y -qq "${PKGS_TO_INSTALL[@]}"
            ;;
        yum)
            PKGS_TO_INSTALL=()
            for p in "${NEED_PKGS[@]}"; do
                if [ "$p" = "__ssh_client__" ]; then
                    PKGS_TO_INSTALL+=("openssh-clients")
                else
                    PKGS_TO_INSTALL+=("$p")
                fi
            done
            yum install -y -q "${PKGS_TO_INSTALL[@]}"
            ;;
        dnf)
            PKGS_TO_INSTALL=()
            for p in "${NEED_PKGS[@]}"; do
                if [ "$p" = "__ssh_client__" ]; then
                    PKGS_TO_INSTALL+=("openssh-clients")
                else
                    PKGS_TO_INSTALL+=("$p")
                fi
            done
            dnf install -y -q "${PKGS_TO_INSTALL[@]}"
            ;;
        apk)
            PKGS_TO_INSTALL=()
            for p in "${NEED_PKGS[@]}"; do
                if [ "$p" = "__ssh_client__" ]; then
                    PKGS_TO_INSTALL+=("openssh-client")
                else
                    PKGS_TO_INSTALL+=("$p")
                fi
            done
            apk add --no-cache "${PKGS_TO_INSTALL[@]}"
            ;;
    esac
fi

echo "[2/6] Fetching source from GitHub..."
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "  -> Updating existing installation at ${INSTALL_DIR}..."
    git -C "$INSTALL_DIR" fetch origin
    git -C "$INSTALL_DIR" reset --hard "origin/${BRANCH}"
else
    if [ -d "$INSTALL_DIR" ]; then
        echo "  -> Existing non-git directory found. Backing up user data..."
        BACKUP_DIR=$(mktemp -d)
        [ -f "$INSTALL_DIR/config.json" ] && cp "$INSTALL_DIR/config.json" "$BACKUP_DIR/"
        [ -f "$INSTALL_DIR/intervals.json" ] && cp "$INSTALL_DIR/intervals.json" "$BACKUP_DIR/"
        [ -d "$INSTALL_DIR/logs" ] && cp -r "$INSTALL_DIR/logs" "$BACKUP_DIR/"
        rm -rf "$INSTALL_DIR"
        git clone -b "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
        echo "  -> Restoring user data..."
        [ -f "$BACKUP_DIR/config.json" ] && cp "$BACKUP_DIR/config.json" "$INSTALL_DIR/"
        [ -f "$BACKUP_DIR/intervals.json" ] && cp "$BACKUP_DIR/intervals.json" "$INSTALL_DIR/"
        [ -d "$BACKUP_DIR/logs" ] && cp -r "$BACKUP_DIR/logs" "$INSTALL_DIR/"
        rm -rf "$BACKUP_DIR"
    else
        echo "  -> Cloning into ${INSTALL_DIR}..."
        mkdir -p "$(dirname "$INSTALL_DIR")"
        git clone -b "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
    fi
fi

mkdir -p "$INSTALL_DIR/public" "$INSTALL_DIR/logs"
chmod +x "$INSTALL_DIR/cron_runner.sh"
chmod +x "$INSTALL_DIR/server.py"

echo "[3/6] Creating systemd service..."
# Tailscale Serve が TLS 終端用に <Tailscale IP>:PORT をバインドできるよう、
# Tailscale 接続環境ではアプリを 127.0.0.1 のみで待機させる
HOST_ENV=""
if [ "${TS_HTTPS}" = "1" ]; then
    HOST_ENV="Environment=HOST=127.0.0.1"
fi
cat > "/etc/systemd/system/${SERVICE_NAME}.service" << SERVICEEOF
[Unit]
Description=rsyncGUI - Web-based rsync interface
After=network.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/server.py
Restart=always
RestartSec=5
TimeoutStopSec=5
Environment=TZ=Asia/Tokyo
Environment=PORT=${PORT}
${HOST_ENV}

[Install]
WantedBy=multi-user.target
SERVICEEOF

echo "Enabling and starting service..."
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"
systemctl restart "${SERVICE_NAME}.service"

sleep 1
if ! systemctl is-active --quiet "${SERVICE_NAME}.service"; then
    echo ""
    echo "=== Installation Failed ==="
    echo "WARNING: Service failed to start. Check with: systemctl status ${SERVICE_NAME}"
    exit 1
fi

# ── Tailscale Serve で HTTPS 公開（冪等）──
SERVE_URL=""
if [ "${TS_HTTPS}" = "1" ]; then
    echo "[Tailscale] Setting up Serve (HTTPS)..."
    tailscale serve --https=${PORT} off >/dev/null 2>&1 || true
    if tailscale serve --bg --https=${PORT} "http://127.0.0.1:${PORT}" >/dev/null 2>&1; then
        for i in $(seq 1 12); do
            HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "https://${TS_DOMAIN}:${PORT}/" 2>/dev/null || echo "000")
            [ "${HTTP_CODE}" != "000" ] && break
            sleep 5
        done
        SERVE_URL="https://${TS_DOMAIN}:${PORT}"
    else
        echo "  [WARN] tailscale serve setup failed (HTTP only)"
    fi
fi

echo ""
echo "=== Installation Complete ==="
if [ -n "${SERVE_URL}" ]; then
    echo "rsyncGUI is running at ${SERVE_URL}  (Tailscale Serve / HTTPS)"
else
    echo "rsyncGUI is running at http://localhost:${PORT}"
fi
echo ""
echo "To check status: systemctl status ${SERVICE_NAME}"
echo "To view logs: journalctl -u ${SERVICE_NAME} -f"

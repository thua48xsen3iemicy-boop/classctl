#!/usr/bin/env bash
set -euo pipefail
umask 022
action=$1
shift
base=/opt/classctl
etc=/etc/classctl
var=/var/lib/classctl
python_bin() {
    command -v python3.12 || command -v python3
}
service_install() {
    local name=$1 account=$2 command=$3 args=$4
    if [[ -d /run/systemd/system ]]; then
        cat > "/etc/systemd/system/$name.service" <<EOF
[Unit]
Description=classctl $name
After=network.target
[Service]
User=$account
ExecStart=$command $args
Restart=on-failure
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF
        systemctl daemon-reload
        systemctl enable "$name"
        systemctl restart "$name"
        systemctl is-active --quiet "$name"
    else
        cat > "/etc/init.d/$name" <<EOF
#!/sbin/openrc-run
description="classctl $name"
command="$command"
command_args="$args"
command_user="$account"
supervisor=supervise-daemon
respawn_delay=3
depend() { need net; }
EOF
        chmod 755 "/etc/init.d/$name"
        rc-update add "$name" default
        rc-service "$name" restart
    fi
}
case "$action" in
prepare)
    role=$1
    if command -v apt-get >/dev/null; then
        packages=(openssl ca-certificates git python3-venv)
        if [[ $role == student ]]; then packages+=(x11-xserver-utils x11-xkb-utils); fi
        if [[ $role == server ]]; then packages+=(curl); command -v chromium >/dev/null || packages+=(chromium); fi
        apt-get update >&2
        DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[@]}" >&2
    elif command -v emerge >/dev/null; then
        packages=(dev-libs/openssl dev-vcs/git)
        if [[ $role == student ]]; then packages+=(x11-apps/xrandr x11-apps/xkbcomp); fi
        if [[ $role == server ]]; then packages+=(net-misc/curl); command -v chromium >/dev/null || packages+=(www-client/chromium); fi
        emerge --noreplace "${packages[@]}" >&2
    fi
    command -v openssl python3 >/dev/null
    command -v git >/dev/null
    [[ -d /run/systemd/system ]] || command -v rc-service >/dev/null
    install -d -m 755 "$base" "$var"
    if [[ $role == student ]]; then
        for cmd in busctl xrandr xkbcomp; do command -v "$cmd" >/dev/null; done
        busctl --system introspect org.freedesktop.login1 /org/freedesktop/login1 >/dev/null
    fi
    install -d -m 755 "$etc"
    ;;
release)
    release=$base/$1
    if [[ ! -e $release/.ready ]]; then
        if [[ ! -d $release/.git ]]; then
            git clone --no-checkout "$var/$1.bundle" "$release"
        fi
        git -C "$release" checkout --detach "$1"
        "$(python_bin)" -m venv "$release/.venv"
        "$release/.venv/bin/pip" install -r "$release/ops/requirements-live.txt" >&2
        touch "$release/.ready"
    fi
    ;;
csr)
    folder=$1
    [[ -d $folder ]] || install -d -m 700 "$folder"
    umask 077
    if [[ ! -e $folder/key.pem ]]; then
        openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$folder/key.new"
        mv "$folder/key.new" "$folder/key.pem"
    fi
    openssl req -new -key "$folder/key.pem" -subj "/CN=$2"
    ;;
inventory)
    release=$base/$1
    install -d -m 700 "$var/coordinator"
    "$release/.venv/bin/python" - "$release/server" "$var/coordinator/room.sqlite" "$etc/room.json" <<'PY'
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from coordinator import inventory
seed = Path(sys.argv[3])
if inventory(sys.argv[2], seed) != json.loads(seed.read_text()):
    sys.exit("Existing SQLite inventory differs. Reconcile seats before deployment.")
PY
    ;;
server)
    release=$base/$1
    getent group classctl-server >/dev/null || groupadd --system classctl-server
    id classctl-server >/dev/null 2>&1 || useradd --system --gid classctl-server --no-create-home classctl-server
    chown classctl-server "$var/coordinator" "$var/coordinator/room.sqlite"
    chgrp classctl-server "$etc/server" "$etc/server/key.pem"
    chmod 750 "$etc/server"
    chmod 640 "$etc/server/key.pem"
    service_install classctl-server classctl-server "$release/.venv/bin/python" \
        "$release/server/live.py --host 0.0.0.0 --port $2 --ui-port $3 --db $var/coordinator/room.sqlite --policy $etc/policy.json --ca $etc/server/ca.pem --cert $etc/server/cert.pem --key $etc/server/key.pem"
    ;;
student)
    release=$base/$1
    user=$2 node=$3 url=$4
    uid=$(id -u "$user")
    home=$(getent passwd "$user" | cut -d: -f6)
    [[ $uid != 0 && -d $home ]]
    "$release/.venv/bin/python" -c 'from PIL import features; import sys; sys.exit(0 if features.check_feature("xcb") else "Pillow requires XCB")'
    install -d -m 755 "$base/bin"
    cat > "$base/bin/node-start" <<EOF
#!/bin/sh
set -eu
install -d -m 755 /run/classctl
exec $release/.venv/bin/python $release/node/live.py --server $url --node $node --socket /run/classctl/seat0.sock --uid $uid --ca $etc/node/ca.pem --cert $etc/node/cert.pem --key $etc/node/key.pem
EOF
    chmod 755 "$base/bin/node-start"
    # Write inside the home directory exclusively with that user's privileges.
    runuser -u "$user" -- mkdir -p "$home/.config/autostart"
    runuser -u "$user" -- tee "$home/.config/autostart/classctl.desktop" >/dev/null <<EOF
[Desktop Entry]
Type=Application
Name=classctl — аудитория
Exec=$release/.venv/bin/python $release/session/live.py --socket /run/classctl/seat0.sock --node $node
OnlyShowIn=KDE;
EOF
    service_install classctl-node root "$base/bin/node-start" ""
    ;;
check)
    url=$1
    shift
    for attempt in {1..20}; do
        if curl --noproxy '*' -fsS --max-time 4 "$url/state" | python3 -c '
import json, sys
nodes = json.load(sys.stdin)["nodes"]
offline = [n for n in sys.argv[1:] if nodes.get(n, {}).get("availability") != "online"]
if offline:
    sys.exit("Waiting for: " + ", ".join(offline))
' "$@"; then exit 0; fi
        sleep 1
    done
    exit 1
    ;;
*) exit 2 ;;
esac

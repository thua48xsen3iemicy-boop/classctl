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
respawn_max=10
respawn_period=60
depend() { use net; }
EOF
        chmod 755 "/etc/init.d/$name"
        rc-update add "$name" default >&2
        rc-service "$name" restart >&2
        rc-service "$name" status >&2
    fi
}
session_desktop() {
    local exec_line=$1
    cat <<EOF
[Desktop Entry]
Type=Application
Name=classctl — аудитория
Exec=$exec_line
OnlyShowIn=KDE;
EOF
}

migrate_autostart() {
    local launcher=$base/bin/node-start old_release old_uid old_node account user directory legacy
    [[ -f $launcher ]] || return 0
    old_uid=$(sed -nE 's/.* --uid ([1-9][0-9]*) --ca .*/\1/p' "$launcher")
    [[ -n $old_uid ]] || return 0
    old_release=$(sed -nE 's|^exec (/opt/classctl/[0-9a-f]+)/[.]venv/bin/python .*|\1|p' "$launcher")
    old_node=$(sed -nE 's/.* --node ([A-Za-z0-9._-]+) --socket .*/\1/p' "$launcher")
    [[ -n $old_release && -n $old_node ]] || return 0
    account=$(getent passwd "$old_uid") || return 0
    IFS=: read -r user _ _ _ _ directory _ <<< "$account"
    legacy=$directory/.config/autostart/classctl.desktop
    if runuser -u "$user" -- test -f "$legacy" &&
        session_desktop "$old_release/.venv/bin/python $old_release/session/live.py --socket /run/classctl/seat0.sock --node $old_node" |
            runuser -u "$user" -- cmp -s "$legacy" -; then
        runuser -u "$user" -- rm -- "$legacy"
    fi
}

case "$action" in
existing-room)
    database=$var/coordinator/room.sqlite
    if [[ -e $database ]]; then
        python3 - "$database" <<'PY'
import json
import sqlite3
import sys
from pathlib import Path

database = Path(sys.argv[1]).as_uri()
with sqlite3.connect(f"{database}?mode=ro", uri=True) as db:
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inventory'").fetchone():
        row = db.execute("SELECT room FROM inventory WHERE id=1").fetchone()
        if row:
            print(json.loads(row[0])["roomId"])
PY
    fi
    ;;
prepare)
    role=$1
    required=(openssl git python3)
    if [[ $role == student ]]; then
        required+=(busctl xrandr xkbcomp)
    else
        required+=(curl)
    fi
    if [[ -d /run/systemd/system ]]; then
        required+=(systemctl)
    else
        required+=(openrc-run rc-service rc-update supervise-daemon)
    fi
    missing=()
    for cmd in "${required[@]}"; do
        command -v "$cmd" >/dev/null || missing+=("$cmd")
    done
    if (( ${#missing[@]} )); then
        printf 'Missing commands on %s (%s): %s\n' "$(hostname)" "$role" "${missing[*]}" >&2
        exit 1
    fi
    python=$(python_bin)
    if ! "$python" -c 'import venv' || ! "$python" -m ensurepip --version >/dev/null; then
        printf 'Python at %s on %s requires venv and ensurepip\n' "$python" "$(hostname)" >&2
        exit 1
    fi
    if [[ $role == student ]]; then
        if ! busctl --system introspect org.freedesktop.login1 /org/freedesktop/login1 >/dev/null; then
            printf 'login1 is unavailable on %s\n' "$(hostname)" >&2
            exit 1
        fi
    fi
    install -d -m 755 "$base" "$var" "$etc"
    ;;
release)
    release=$base/$1
    if [[ -e $release/.ready && -d $release/.venv && ! -L $release/.venv ]]; then
        # Keep the existing environment for releases created by the old installer.
        exit 0
    fi
    if [[ ! -e $release/.ready ]]; then
        if [[ ! -d $release/.git ]]; then
            git clone --no-checkout "$var/$1.bundle" "$release"
        fi
        git -C "$release" checkout --detach "$1"
    fi
    if [[ -v classctl_pip_proxy ]]; then
        export PIP_PROXY="$classctl_pip_proxy"
    fi
    if [[ -v classctl_pip_index_url ]]; then
        export PIP_INDEX_URL="$classctl_pip_index_url"
    fi
    if [[ -v classctl_pip_trusted_host ]]; then
        export PIP_TRUSTED_HOST="${PIP_TRUSTED_HOST:+$PIP_TRUSTED_HOST }$classctl_pip_trusted_host"
    fi
    "$(python_bin)" "$release/ops/ssh/prepare_venv.py" "$release" "$base/venvs" >&2
    touch "$release/.ready"
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
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from coordinator import inventory
try:
    inventory(sys.argv[2], Path(sys.argv[3]), extend=True)
except ValueError as error:
    sys.exit(str(error))
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
    node=$2 url=$3
    "$release/.venv/bin/python" -c 'from PIL import features; import sys; sys.exit(0 if features.check_feature("xcb") else "Pillow requires XCB")'
    install -d -m 755 "$base/bin"
    install -d -m 700 "$var/node"
    install -d -m 755 /etc/xdg/autostart
    migrate_autostart
    cat > "$base/bin/session-start" <<EOF
#!/bin/sh
set -eu
release=\$(readlink -f "$base/current-session")
exec "\$release/.venv/bin/python" "\$release/session/live.py" --socket /run/classctl/seat0.sock --node $node --release-link "$base/current-session"
EOF
    chmod 755 "$base/bin/session-start"
    cat > "$base/bin/node-start" <<EOF
#!/bin/sh
set -eu
install -d -m 755 /run/classctl
exec $release/.venv/bin/python $release/node/live.py --server $url --node $node --socket /run/classctl/seat0.sock --ca $etc/node/ca.pem --cert $etc/node/cert.pem --key $etc/node/key.pem --power-db $var/node/power.sqlite
EOF
    chmod 755 "$base/bin/node-start"
    service_install classctl-node root "$base/bin/node-start" ""
    staging=$(mktemp -d "$base/.session-release.XXXXXX")
    ln -s "$release" "$staging/current-session"
    mv -Tf "$staging/current-session" "$base/current-session"
    rmdir "$staging"
    session_desktop "$base/bin/session-start" > /etc/xdg/autostart/classctl.desktop
    ;;
check)
    url=$1
    shift
    previous=
    for attempt in {1..20}; do
        if ! status=$(curl --noproxy '*' -fsS --max-time 4 -o /dev/null "$url/" 2>&1); then
            status="Waiting for portal at $url/ ($status)"
        elif response=$(curl --noproxy '*' -fsS --max-time 4 "$url/state" 2>&1); then
            if status=$(python3 -c '
import json, sys
try:
    nodes = json.load(sys.stdin)["nodes"]
    offline = [n for n in sys.argv[1:] if nodes.get(n, {}).get("availability") != "online"]
except (ValueError, KeyError, TypeError, AttributeError):
    print("Waiting for a valid /state response")
    sys.exit(1)
if offline:
    print("Waiting for: " + ", ".join(offline))
    sys.exit(1)
' "$@" <<< "$response"); then
                exit 0
            fi
        else
            status="Waiting for $url/state ($response)"
        fi
        if [[ $status != "$previous" ]]; then
            printf '%s\n' "$status" >&2
            previous=$status
        fi
        sleep 1
    done
    printf 'Readiness check timed out: %s\n' "$previous" >&2
    exit 1
    ;;
*) exit 2 ;;
esac

#!/bin/sh
# Runs as root only long enough to:
#   1. make the data folder writable by the app user (bind mounts are often root-owned), and
#   2. give the app user the group that owns the Docker socket, whatever its ID is on this host,
# then drops to the unprivileged `dockllama` user. This replaces v1's manual --group-add step.
set -e

APP_USER=dockllama
DATA="${DOCKLLAMA_DATA:-/app/data}"
SOCK=/var/run/docker.sock

if [ "$(id -u)" != "0" ]; then
    # Started with an explicit non-root user; nothing to fix up.
    exec "$@"
fi

# Optional PUID/PGID (the usual homelab convention, e.g. 99/100 on Unraid): run as those
# IDs so files in a bind-mounted data folder get the owner the user expects.
require_number() {  # name value
    case "$2" in
        "" ) ;;
        *[!0-9]* ) echo "entrypoint: $1 must be a number (got '$2')" >&2; exit 1 ;;
    esac
}
require_number PUID "${PUID:-}"
require_number PGID "${PGID:-}"
if [ -n "${PGID:-}" ] && [ "$PGID" != "$(id -g "$APP_USER")" ]; then
    groupmod -o -g "$PGID" "$APP_USER"
fi
if [ -n "${PUID:-}" ] && [ "$PUID" != "$(id -u "$APP_USER")" ]; then
    usermod -o -u "$PUID" "$APP_USER"
fi

mkdir -p "$DATA"
app_uid="$(id -u "$APP_USER")"
if [ "$(stat -c %u "$DATA")" != "$app_uid" ] || [ -n "$(find "$DATA" ! -user "$APP_USER" -print -quit)" ]; then
    chown -R "$APP_USER:$APP_USER" "$DATA"
fi

if [ -S "$SOCK" ]; then
    sock_gid="$(stat -c %g "$SOCK")"
    group="$(getent group "$sock_gid" | cut -d: -f1)"
    if [ -z "$group" ]; then
        groupadd -g "$sock_gid" dockersock
        group=dockersock
    fi
    # On Docker Desktop the socket is root:root (gid 0), so this adds the root *group*;
    # the process still runs as the unprivileged dockllama user.
    usermod -aG "$group" "$APP_USER"
    echo "entrypoint: Docker socket group $group (gid $sock_gid) granted to $APP_USER"
else
    echo "entrypoint: no Docker socket at $SOCK; DockLlama won't see any containers on this machine"
fi

exec setpriv --reuid="$APP_USER" --regid="$APP_USER" --init-groups "$@"

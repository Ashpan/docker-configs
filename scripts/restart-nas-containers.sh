#!/usr/bin/env bash
# Restart Docker containers that started before their NAS mount was available
# (power loss, NAS booting slower than the server, etc.). Such a container
# bound the empty local dir and never sees the NFS mount, so we check each
# container's own mount table and restart only the stale ones.
# Runs every minute via systemd/nas-recover.timer; safe to run by hand (as root).
set -euo pipefail

COMPOSE_DIR="/home/ashpan/docker"

# Map: mount point -> containers that depend on it
declare -A MOUNT_CONTAINERS
MOUNT_CONTAINERS["/mnt/pegasus"]="jellyfin qbittorrent radarr sonarr_tv bazarr audiobookshelf"
MOUNT_CONTAINERS["/mnt/immich"]="immich-server immich-machine-learning"
MOUNT_CONTAINERS["/mnt/nextcloud"]="nextcloud"

# How long (seconds) to wait for a mount to become accessible before giving up
MOUNT_TIMEOUT=5

# ---

log()  { echo "[$(date '+%H:%M:%S')] $*"; }
warn() { echo "[$(date '+%H:%M:%S')] WARN: $*" >&2; }

mount_accessible() {
  local mnt="$1"
  mountpoint -q "$mnt" 2>/dev/null || return 1
  timeout "$MOUNT_TIMEOUT" ls "$mnt" > /dev/null 2>&1
}

stale=()

for mnt in "${!MOUNT_CONTAINERS[@]}"; do
  # A boot-time mount that failed (NAS down) is never retried on its own
  mountpoint -q "$mnt" || timeout 30 mount "$mnt" > /dev/null 2>&1 || true

  if ! mount_accessible "$mnt"; then
    warn "$mnt is NOT accessible — skipping: ${MOUNT_CONTAINERS[$mnt]}"
    continue
  fi

  fstype=$(findmnt -no FSTYPE "$mnt")
  for c in ${MOUNT_CONTAINERS[$mnt]}; do
    pid=$(docker inspect -f '{{.State.Pid}}' "$c" 2>/dev/null) || continue
    [[ "$pid" -gt 0 ]] || continue  # not running; leave it alone
    grep -q " - $fstype " "/proc/$pid/mountinfo" || stale+=("$c")
  done
done

[[ ${#stale[@]} -eq 0 ]] && exit 0

# Deduplicate
mapfile -t stale < <(printf '%s\n' "${stale[@]}" | sort -u)

log "Restarting ${#stale[*]} stale container(s): ${stale[*]}"

cd "$COMPOSE_DIR"
docker compose restart "${stale[@]}"

log "Done."

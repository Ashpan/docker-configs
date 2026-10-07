#!/usr/bin/env bash
# Restart Docker containers whose volumes depend on NFS-mounted NAS paths.
# Run this after a NAS comes back up (power loss, reboot, etc.) to recover
# containers that started before the mounts were available.
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

containers_to_restart=()

for mnt in "${!MOUNT_CONTAINERS[@]}"; do
  if mount_accessible "$mnt"; then
    log "$mnt is accessible"
    # shellcheck disable=SC2206
    containers_to_restart+=(${MOUNT_CONTAINERS[$mnt]})
  else
    warn "$mnt is NOT accessible — skipping: ${MOUNT_CONTAINERS[$mnt]}"
  fi
done

if [[ ${#containers_to_restart[@]} -eq 0 ]]; then
  warn "No NAS mounts are accessible. Nothing to restart."
  exit 1
fi

# Deduplicate
mapfile -t containers_to_restart < <(printf '%s\n' "${containers_to_restart[@]}" | sort -u)

log "Restarting ${#containers_to_restart[*]} container(s): ${containers_to_restart[*]}"

cd "$COMPOSE_DIR"
docker compose restart "${containers_to_restart[@]}"

log "Done."

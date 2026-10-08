#!/bin/sh

GREEN="$(printf '\033[0;32m')"
RESET="$(printf '\033[0m')"

log() {
    printf "%s================ %s ================%s\n" "$GREEN" "$1" "$RESET"
}

log "Stopping compose services and removing network/volumes..."
podman compose down -v

log "Removing any remaining weddingstack volumes..."
podman volume ls -q | grep -i 'zwift' | xargs -r podman volume rm -f

log "Removing any remaining unused images..."
podman system prune -a -f

log "Rebuilding and starting services from scratch..."
podman compose up -d --build --no-cache

log "Reset complete."
log "Press Enter to close..."
read -r _


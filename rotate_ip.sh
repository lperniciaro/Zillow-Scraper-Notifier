#!/bin/sh
# =============================================================================
# rotate_ip.sh  –  Gluetun IP Rotator Sidecar
# =============================================================================
# Periodically kills and restarts the Gluetun VPN tunnel so that PIA assigns
# a fresh exit-node IP.  This is done by hitting Gluetun's built-in control
# server REST API (port 8000) to stop/start the VPN engine.
#
# Environment variables:
#   ROTATE_INTERVAL   seconds between rotations  (default: 300 = 5 min)
#   GLUETUN_HOST      hostname of gluetun         (default: gluetun)
#   GLUETUN_PORT      control server port         (default: 8000)
# =============================================================================

ROTATE_INTERVAL="${ROTATE_INTERVAL:-300}"
GLUETUN_HOST="${GLUETUN_HOST:-gluetun}"
GLUETUN_PORT="${GLUETUN_PORT:-8000}"
CONTROL="http://${GLUETUN_HOST}:${GLUETUN_PORT}"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [rotate_ip] $*"
}

wait_for_gluetun() {
    log "Waiting for Gluetun control server at ${CONTROL}..."
    until wget -qO- "${CONTROL}/v1/openvpn/status" > /dev/null 2>&1; do
        sleep 5
    done
    log "Gluetun is up."
}

rotate() {
    log "Rotating VPN IP..."

    # Stop the VPN tunnel
    wget -qO- --method=PUT \
        --header="Content-Type: application/json" \
        --body-data='{"status":"stopped"}' \
        "${CONTROL}/v1/openvpn/status" > /dev/null 2>&1

    sleep 5

    # Start it again – PIA will assign a new server / IP
    wget -qO- --method=PUT \
        --header="Content-Type: application/json" \
        --body-data='{"status":"running"}' \
        "${CONTROL}/v1/openvpn/status" > /dev/null 2>&1

    log "Rotation request sent. Waiting for tunnel to re-establish..."
    sleep 20

    # Confirm new IP
    NEW_IP=$(wget -qO- "${CONTROL}/v1/publicip/ip" 2>/dev/null | grep -o '"public_ip":"[^"]*"' | cut -d'"' -f4)
    if [ -n "$NEW_IP" ]; then
        log "New public IP: ${NEW_IP}"
    else
        log "Could not confirm new IP (tunnel may still be connecting)."
    fi
}

# ---------------------------------------------------------------------------
main() {
    wait_for_gluetun

    # Log the initial IP
    INIT_IP=$(wget -qO- "${CONTROL}/v1/publicip/ip" 2>/dev/null | grep -o '"public_ip":"[^"]*"' | cut -d'"' -f4)
    log "Initial public IP: ${INIT_IP:-unknown}"

    log "IP rotation sidecar started. Rotating every ${ROTATE_INTERVAL}s."

    while true; do
        sleep "${ROTATE_INTERVAL}"
        rotate
    done
}

main

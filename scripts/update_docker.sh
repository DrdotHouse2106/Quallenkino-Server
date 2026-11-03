#!/bin/bash
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export COMPOSE_PARALLEL_LIMIT=2   # nicht alles gleichzeitig ziehen (Rate-Limit der Registries)
cd "${STACK_DIR:-/opt/media-stack}" || exit 1

# Optional: Push an ntfy, wenn das Update nicht klappt (NTFY_URL z. B. http://127.0.0.1:8090/thema,
# NTFY_TOKEN ein Zugriffstoken). Ohne NTFY_URL wird nichts gesendet.
melden() {
    [ -n "$NTFY_URL" ] || return
    curl -s -m 15 -o /dev/null ${NTFY_TOKEN:+-H "Authorization: Bearer $NTFY_TOKEN"} \
        -H "Title: $1" -H "Priority: 4" -H "Tags: warning" -d "$2" "$NTFY_URL"
}

echo "=== $(date) ==="

# Docker-Bilder aktualisieren. Ist eine Registry kurz weg (z. B. TLS-Timeout),
# bis zu 8 Versuche über knapp eine Stunde; danach Push-Meldung und mit dem weiter, was da ist.
for i in 1 2 3 4 5 6 7 8; do
    docker compose pull && break
    if [ "$i" = 8 ]; then
        echo "Pull nach 8 Versuchen fehlgeschlagen"
        melden "Container-Update unvollständig" "docker compose pull ist 8 Mal fehlgeschlagen. Aktualisiert wird nur, was geladen werden konnte."
        break
    fi
    echo "Pull fehlgeschlagen (Versuch $i), warte $((i * 2)) min..."
    sleep $((i * 120))
done

# Container neu starten
docker compose up -d

# Unnötigen Datenmüll entfernen
docker image prune -f

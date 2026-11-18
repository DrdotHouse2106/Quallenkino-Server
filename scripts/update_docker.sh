#!/bin/bash
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export COMPOSE_PARALLEL_LIMIT=2   # nicht alles gleichzeitig ziehen (Rate-Limit der Registries)
cd "${STACK_DIR:-/opt/media-stack}" || exit 1

# Optional: Push an ntfy, wenn das Update nicht klappt (NTFY_URL z. B. http://127.0.0.1:8090/thema,
# NTFY_TOKEN ein Zugriffstoken). Ohne NTFY_URL wird nichts gesendet.
melden() {
    [ -n "$NTFY_URL" ] || return
    local kopf=()
    # Token über eine Kopfzeilen-Datei, damit es nicht in der Prozessliste (ps) steht
    [ -n "$NTFY_TOKEN" ] && kopf=(-H @<(printf 'Authorization: Bearer %s\n' "$NTFY_TOKEN"))
    curl -s -m 15 -o /dev/null "${kopf[@]}" \
        -H "Title: $1" -H "Priority: 4" -H "Tags: warning" -d "$2" "$NTFY_URL"
}

# Rückweg: Das laufende Image jedes Dienstes als rollback/<dienst>:vorher merken (eine Generation).
# Zurück mit: update_docker.sh --zurueck <dienst>. Datenbanken werden dabei nicht zurückgedreht.
images_merken() {
    local dienst c
    for dienst in $(docker compose config --services); do
        c=$(docker compose ps -q "$dienst" 2>/dev/null | head -1)
        [ -n "$c" ] && docker tag "$(docker inspect -f "{{.Image}}" "$c")" "rollback/$dienst:vorher"
    done
}

if [ "$1" = "--zurueck" ]; then
    docker image inspect "rollback/$2:vorher" > /dev/null 2>&1 || { echo "Kein gemerktes Image für $2"; exit 1; }
    docker tag "rollback/$2:vorher" "$(docker compose config --images "$2")"
    docker compose up -d --no-build "$2"
    exit 0
fi

echo "=== $(date) ==="

images_merken

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

# Läuft nach dem Update etwas nicht? Dann melden.
sleep 180
kaputt=$(docker ps -a --filter status=exited --filter status=restarting --format "{{.Names}}"; docker ps --filter health=unhealthy --format "{{.Names}}")
kaputt=$(echo "$kaputt" | sort -u | tr "\n" " ")
if [ -n "${kaputt// /}" ]; then
    echo "Nach dem Update nicht gesund: $kaputt"
    melden "Container nach Update nicht gesund" "$kaputt - zurueck mit: update_docker.sh --zurueck <dienst>"
fi

# Unnötigen Datenmüll entfernen
docker image prune -f

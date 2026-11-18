# 8. Updates, Backups, Fernzugriff

## Updates

[`scripts/update_docker.sh`](../scripts/update_docker.sh) zieht neue Images, startet geänderte
Container neu und räumt alte Images auf. Höchstens 2 Images werden parallel geladen (Rate-Limits).
Schlägt der Download fehl, versucht es das Skript bis zu 8 Mal über knapp eine Stunde. Danach
schickt es auf Wunsch eine Push-Meldung über ntfy (`NTFY_URL`, optional `NTFY_TOKEN` setzen).

Die linuxserver-Images kommen direkt von `ghcr.io/linuxserver/…`. `lscr.io` leitet nur dorthin
weiter und war bei uns zeitweise nicht erreichbar (TLS-Timeout); die Images sind identisch.

**Rückweg:** Vor jedem Update merkt sich das Skript das laufende Image als
`rollback/<dienst>:vorher`. Macht ein Update etwas kaputt, reicht
`scripts/update_docker.sh --zurueck sonarr`. Datenbanken werden dabei nicht zurückgedreht. Ist nach
dem Update ein Container nicht gesund, kommt eine ntfy-Meldung, wenn `NTFY_URL` gesetzt ist.

```
0 4 * * 0 /opt/media-stack/scripts/update_docker.sh >> /opt/media-stack/update_docker.log 2>&1
```

Vorsicht bei `:latest`: Ein Update kann Datenbanken migrieren. Vor großen Versionssprüngen
(z. B. Jellyfin) ein Backup machen und die Release Notes lesen.

## Backups

Wichtig ist nur `config/` – Medien lassen sich neu laden, Einstellungen, Gesehen-Status und
Datenbanken nicht. Am einfachsten: Container stoppen, `config/` packen, starten.
Unter Proxmox reicht ein regelmäßiger Snapshot/Backup des LXC.

Vor Änderungen an `docker-compose.yml` eine Kopie anlegen
(`cp docker-compose.yml docker-compose.yml.bak-$(date +%Y%m%d)`).

## Fernzugriff

Möglichkeiten, von unterwegs auf Jellyfin zu kommen:

| Weg | Vorteil | Nachteil |
|---|---|---|
| **VPN** (WireGuard, Tailscale) | nichts offen im Internet | App/VPN auf jedem Gerät |
| **Reverse Proxy** mit eigener Domain + HTTPS (Caddy, Nginx Proxy Manager, Traefik) | funktioniert überall, auch auf Fernsehern bei Freunden | Port offen, Updates wichtig |
| Reverse Proxy auf einem **gemieteten Server** (z. B. bei DS-Lite ohne eigene IPv4) | geht auch ohne öffentliche IPv4 | zusätzlicher Umweg, langsamer als im Heimnetz |

Bei wechselnder IP (v. a. IPv6) braucht es DynDNS. Die meisten DNS-Anbieter haben dafür eine API;
ein kleines Skript per Cron prüft die aktuelle Adresse und aktualisiert den Eintrag nur bei Änderung.

Im Heimnetz sollten Apps direkt die lokale Adresse nutzen – über die Domain läuft jeder Film
sonst einmal durchs Internet und zurück.

Nicht ins Internet stellen: die *arr-Dienste, SABnzbd, Prowlarr, m3u-editor, Shoko.

### Docker und die Firewall (ufw)

Ports, die Docker veröffentlicht (`ports:` in der Compose-Datei), **umgehen ufw**: Docker trägt eigene
iptables-Regeln vor denen von ufw ein. `ufw deny 8989` schützt Sonarr also nicht. Deshalb binden in der
Vorlage alle Verwaltungsdienste an `BIND_ADDR` aus der `.env` (Standard `127.0.0.1`, für das Heimnetz die
LAN-Adresse des Servers). Allgemein erreichbar sind nur die Dienste für Zuschauer: Jellyfin, Jellyseerr,
Navidrome, Audiobookshelf und Kavita. Wer den Server mit öffentlicher Adresse betreibt (gemieteter Server,
IPv6 ohne Router-Firewall), sollte auch diese nur über VPN oder Reverse Proxy freigeben.

### Images und automatische Updates

Alle Images stehen auf `latest`, `update_docker.sh` holt sonntags neue Versionen. Das ist im Heimbetrieb
üblich, heißt aber auch: Ein manipuliertes Image käme automatisch auf den Server. Bei kleinen Projekten
mit wenigen Betreuern (z. B. `lexfi/umlautadaptarrex`, `nandyalu/trailarr`, `sparkison/m3u-editor` und
`sparkison/m3u-proxy`) kann man die Version festlegen (`image: name:1.2.3` oder `name@sha256:…`) und
Updates bewusst einspielen. Zurück auf die vorige Version geht mit `update_docker.sh --zurueck <dienst>`.

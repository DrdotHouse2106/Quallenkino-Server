#!/bin/sh
# EPG von MagentaTV über iptv-org/epg holen (14 Tage), alle 12 Stunden.
# Ergebnis: /work/magenta.xml – enrich_epg.py führt es mit epgshare01 zusammen.
cd /work/epg || exit 1
if [ ! -d node_modules ]; then
  npm ci --ignore-scripts --no-audit --no-fund > /work/npm.log 2>&1 || { echo "$(date '+%F %T') FEHLER: npm ci" >> /work/grab.log; sleep 3600; exit 1; }
fi
while true; do
  rm -f /work/magenta.tmp.xml
  if npm run grab --- --channels=/work/magenta.channels.xml --days=14 --maxConnections=1 \
       --output=/work/magenta.tmp.xml > /work/grab-letzter-lauf.log 2>&1 && [ -s /work/magenta.tmp.xml ]; then
    # nur aktualisieren: leere Sender-Tage aus dem letzten Stand übernehmen
    if [ -s /work/magenta.xml ] && node /work/merge.mjs /work/magenta.xml /work/magenta.tmp.xml /work/magenta.merged.xml >> /work/grab-letzter-lauf.log 2>&1; then
      mv /work/magenta.merged.xml /work/magenta.tmp.xml
    fi
    # grep -c gibt bei 0 Treffern Exit 1, "|| echo 0" ergäbe dann "0<Zeilenumbruch>0" und beendet sh -> wc -l
    neu=$(grep -o '<programme' /work/magenta.tmp.xml | wc -l); alt=$(grep -o '<programme' /work/magenta.xml 2>/dev/null | wc -l)
    # MagentaTV liefert bei Drosselung leere Tage: deutlich kleinere Ergebnisse nicht übernehmen
    if [ "$neu" -eq 0 ] || [ "$neu" -lt $((alt * 8 / 10)) ]; then
      echo "$(date '+%F %T') VERWORFEN: nur $neu statt $alt Sendungen - letzte Datei bleibt" >> /work/grab.log
    else
      mv /work/magenta.tmp.xml /work/magenta.xml
      echo "$(date '+%F %T') OK: $neu Sendungen" >> /work/grab.log
    fi
  else
    echo "$(date '+%F %T') FEHLER, siehe grab-letzter-lauf.log – letzte Datei bleibt" >> /work/grab.log
  fi
  sleep 43200
done

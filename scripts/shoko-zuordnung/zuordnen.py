#!/usr/bin/env python3
"""Ordnet unerkannte Dateien in Shoko mit Hilfe von Claude einer Folge zu.

Läuft als systemd-Timer auf media-stack. Fragt Claude nur, wenn es eine neue
unerkannte Datei gibt. Claude darf in Shoko nur lesen und meldet eine
Entscheidung; zugeordnet wird nur bei Sicherheit "hoch", alles andere landet
in offen.json zur Nachfrage.

Aufruf: zuordnen.py [--nur-zeigen]
Solange es /root/.config/shoko-zuordnung/scharf nicht gibt, wird nur vorgeschlagen.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import anthropic
from anthropic import beta_tool

SHOKO = os.environ.get("SHOKO_URL", "http://localhost:8111")
JELLYFIN = os.environ.get("JELLYFIN_URL", "http://localhost:8096")
KONF = "/root/.config/shoko-zuordnung"
STAND = "/var/lib/shoko-zuordnung"
# Shoko braucht nach dem Import etwas Zeit für Hash und AniDB-Abgleich.
WARTEZEIT = timedelta(minutes=30)
# Unsichere Fälle erst nach 6 Stunden erneut fragen (AniDB lernt neue Folgen und Dateien oft nach).
ERNEUT_NACH = timedelta(hours=6)
# Eine Serie höchstens alle 12 Stunden bei AniDB neu laden, sonst droht eine AniDB-Sperre.
AKTUALISIEREN_SPERRE = timedelta(hours=12)
# Kostenbremse: Dateien pro Lauf, Versuche pro Datei (danach nur noch in offen.json),
# Werkzeugrunden pro Datei und AniDB-Neuladen pro Lauf.
MAX_PRO_LAUF = 10
MAX_VERSUCHE = 4
MAX_RUNDEN = 12
MAX_AKTUALISIERUNGEN = 3
MODELL = "claude-opus-5-5"

NUR_ZEIGEN = "--nur-zeigen" in sys.argv or not os.path.exists(f"{KONF}/scharf")


def lesen(pfad, standard):
    try:
        with open(pfad) as f:
            return json.load(f)
    except FileNotFoundError:
        return standard


def schreiben(pfad, daten):
    tmp = pfad + ".tmp"
    with open(tmp, "w") as f:
        json.dump(daten, f, ensure_ascii=False, indent=2)
    os.replace(tmp, pfad)


def shoko(pfad, methode="GET", daten=None):
    with open(f"{KONF}/shoko-key") as f:
        schluessel = f.read().strip()
    anfrage = urllib.request.Request(
        SHOKO + pfad,
        method=methode,
        headers={"apikey": schluessel, "Content-Type": "application/json"},
        data=json.dumps(daten).encode() if daten is not None else None,
    )
    with urllib.request.urlopen(anfrage, timeout=60) as antwort:
        inhalt = antwort.read()
        return json.loads(inhalt) if inhalt else None


# ------------------------------------------------------------ Werkzeuge für Claude (nur lesend)

@beta_tool
def serien_suchen(suchbegriff: str) -> str:
    """Sucht Serien in der Shoko-Sammlung (nur Serien, die schon in Shoko sind).

    Jede AniDB-Staffel ist bei Shoko eine eigene Serie. Liefert ID, Titel,
    AniDB-ID und Folgenzahl der besten Treffer.

    Args:
        suchbegriff: Titel oder Teil des Titels, englisch, deutsch oder japanisch.
    """
    q = urllib.parse.quote(suchbegriff)
    treffer = shoko(f"/api/v3/Series/Search?query={q}&fuzzy=true&limit=10")
    return json.dumps([
        {
            "SerienID": t["IDs"]["ID"],
            "Titel": t.get("Name"),
            "Treffer": t.get("Match"),
            "AniDB": t["IDs"].get("AniDB"),
            "Folgen": (t.get("Sizes") or {}).get("Total"),
        }
        for t in treffer
    ], ensure_ascii=False)


@beta_tool
def folgen_auflisten(serien_id: int, um_nummer: int | None = None) -> str:
    """Listet die Folgen einer Shoko-Serie und die Dateien, die schon zugeordnet sind.

    Mit um_nummer nur die Folgen in der Nähe dieser Nummer (±5, je Folgenart) und die
    Dateinamen der dazu zugeordneten Dateien. Ohne um_nummer die ganze Liste; die nur
    holen, wenn der Ausschnitt nicht reicht (z. B. durchgehende Zählung über Staffeln).

    Args:
        serien_id: SerienID aus serien_suchen.
        um_nummer: Folgennummer, um die herum gesucht wird (meist die aus dem Dateinamen).
    """
    folgen = shoko(f"/api/v3/Series/{serien_id}/Episode?includeMissing=true&pageSize=500&includeDataFrom=AniDB")["List"]
    dateien = shoko(f"/api/v3/Series/{serien_id}/File?pageSize=500&include=XRefs")["List"]
    gesamt = len(folgen)
    gelistete_folgen.update(f["IDs"]["ID"] for f in folgen)
    if um_nummer is not None:
        folgen = [f for f in folgen if abs(((f.get("AniDB") or {}).get("EpisodeNumber") or 0) - um_nummer) <= 5]
        ids = {f["IDs"]["ID"] for f in folgen}
        dateien = [d for d in dateien
                   if any(e.get("ID") in ids for x in d.get("SeriesIDs") or [] for e in x.get("EpisodeIDs") or [])]
    return json.dumps({
        "FolgenInsgesamt": gesamt,
        "Folgen": [
            {
                "FolgenID": f["IDs"]["ID"],
                "Art": (f.get("AniDB") or {}).get("Type"),
                "Nummer": (f.get("AniDB") or {}).get("EpisodeNumber"),
                "Titel": f.get("Name"),
                "Ausstrahlung": (f.get("AniDB") or {}).get("AirDate"),
                "AnzahlDateien": f.get("Size"),
            }
            for f in folgen
        ],
        "ZugeordneteDateien": [l["RelativePath"].rsplit("/", 1)[-1] for d in dateien for l in d["Locations"]],
    }, ensure_ascii=False)


aktualisiert = lesen(f"{STAND}/aktualisiert.json", {})
aktualisiert_im_lauf = []
# FolgenIDs, die Claude in diesem Lauf gezeigt wurden; nur die dürfen verknüpft werden.
gelistete_folgen = set()


@beta_tool
def serie_von_anidb_aktualisieren(serien_id: int) -> str:
    """Lädt die Daten einer Serie in Shoko neu von AniDB, z. B. wenn eine gerade erschienene Folge in der Folgenliste noch fehlt.

    Nur aufrufen, wenn die passende Folge in folgen_auflisten fehlt. Danach folgen_auflisten erneut aufrufen.
    Pro Serie höchstens alle 12 Stunden möglich.

    Args:
        serien_id: SerienID aus serien_suchen.
    """
    jetzt = datetime.now(timezone.utc)
    zuletzt = aktualisiert.get(str(serien_id))
    if zuletzt and jetzt - datetime.fromisoformat(zuletzt) < AKTUALISIEREN_SPERRE:
        return f"Nicht möglich: Diese Serie wurde zuletzt am {zuletzt} aktualisiert."
    if len(aktualisiert_im_lauf) >= MAX_AKTUALISIERUNGEN:
        return "Nicht möglich: In diesem Lauf wurden schon genug Serien neu geladen. Melde, dass die Folge fehlt."
    aktualisiert_im_lauf.append(serien_id)
    shoko(f"/api/v3/Series/{serien_id}/AniDB/Refresh?force=true&downloadRelations=false&createSeriesEntry=false&immediate=true", "POST")
    aktualisiert[str(serien_id)] = jetzt.isoformat()
    schreiben(f"{STAND}/aktualisiert.json", aktualisiert)
    return "Aktualisiert. Rufe folgen_auflisten erneut auf."


entscheidung = {}


@beta_tool
def entscheidung_melden(folgen_ids: list[int], sicherheit: str, begruendung: str) -> str:
    """Meldet das Ergebnis für die Datei. Genau einmal am Ende aufrufen.

    Args:
        folgen_ids: FolgenIDs aus folgen_auflisten, zu denen die Datei gehört (meist genau eine; leer, wenn nichts passt).
        sicherheit: "hoch" nur, wenn Serie, Staffel und Folge eindeutig belegt sind; sonst "mittel" oder "niedrig".
        begruendung: Kurze Begründung auf Deutsch.
    """
    entscheidung.clear()
    entscheidung.update(folgen_ids=folgen_ids, sicherheit=sicherheit, begruendung=begruendung)
    return "gespeichert"


ANWEISUNG = """Du ordnest in Shoko (Anime-Verwaltung) eine Videodatei, die Shoko nicht automatisch erkannt hat, der richtigen Folge zu.

Die Dateien kommen meist von Sonarr und heißen „Serie/Season N/Serie - SxxEyy - Folgentitel Qualität.mkv“. Sonarr zählt nach TVDB, Shoko nach AniDB: Bei AniDB ist jede Staffel oft eine eigene Serie mit eigener Zählung ab Folge 1, und Specials, Recaps oder OVAs sind eigene Folgenarten. Prüfe deshalb Staffel, Folgennummer, Folgentitel und Ausstrahlungsdatum gegen die Folgenliste, und nutze die schon zugeordneten Dateien derselben Serie als Hinweis auf die Zählung.

Suche mit serien_suchen und folgen_auflisten. Rufe folgen_auflisten zuerst mit um_nummer (der Folgennummer aus dem Dateinamen) auf und hole die ganze Liste nur, wenn der Ausschnitt nicht reicht. Die Folgenliste in Shoko kann veraltet sein: Fehlt die passende Folge (z. B. eine gerade erschienene), lass die Serie mit serie_von_anidb_aktualisieren neu laden und sieh noch einmal nach. Fehlt sie danach immer noch, ordne die Datei keiner anderen Folge zu, sondern melde das. Rufe am Ende genau einmal entscheidung_melden auf. Gib „hoch“ nur, wenn du dir sicher bist; eine falsche Zuordnung ist schlimmer als keine. Ändere nichts anderes."""


def claude_fragen(datei):
    with open(f"{KONF}/anthropic-key") as f:
        client = anthropic.Anthropic(api_key=f.read().strip())
    pfade = [l.get("AbsolutePath") or l["RelativePath"] for l in datei["Locations"]]
    entscheidung.clear()
    gelistete_folgen.clear()
    runner = client.beta.messages.tool_runner(
        model=MODELL,
        max_tokens=16000,
        max_iterations=MAX_RUNDEN,
        system=ANWEISUNG,
        output_config={"effort": "medium"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        tools=[serien_suchen, folgen_auflisten, serie_von_anidb_aktualisieren, entscheidung_melden],
        messages=[{
            "role": "user",
            "content": json.dumps({
                "DateiID": datei["ID"],
                "Pfade": pfade,
                "GrößeGB": round(datei["Size"] / 1e9, 2),
                "ImportiertAm": datei.get("Created"),
            }, ensure_ascii=False),
        }],
    )
    for nachricht in runner:
        if nachricht.stop_reason == "refusal":
            return {"folgen_ids": [], "sicherheit": "niedrig", "begruendung": "Claude hat die Anfrage abgelehnt."}
    return dict(entscheidung) or {"folgen_ids": [], "sicherheit": "niedrig", "begruendung": "Keine Entscheidung gemeldet."}


def jellyfin_anstossen(shoko_serien):
    """Lässt Jellyfin die betroffenen Serien neu einlesen, damit neu zugeordnete Folgen gleich erscheinen.

    Shokofin bekommt Zuordnungen eigentlich über SignalR mit, das klappt aber nicht immer.
    Kennt Jellyfin die Serie noch gar nicht, wird die ganze Shokofin-Bibliothek eingelesen.
    """
    if not os.path.exists(f"{KONF}/jellyfin-key"):
        return
    with open(f"{KONF}/jellyfin-key") as f:
        kopf = {"Authorization": f'MediaBrowser Token="{f.read().strip()}"'}

    def jf(pfad, methode="GET"):
        anfrage = urllib.request.Request(JELLYFIN + pfad, method=methode, headers=kopf)
        with urllib.request.urlopen(anfrage, timeout=60) as antwort:
            inhalt = antwort.read()
            return json.loads(inhalt) if inhalt else None

    # Direkt nach dem Verknüpfen kennt Shokofin die Zuordnung oft noch nicht. Jellyfin legt die Folge
    # dann ohne Shoko-Daten an, mit dem Dateinamen als Titel (Magic Repo Man S01E04, 25.10.).
    time.sleep(90)

    # Shokofin trägt die Shoko-Serie als Kennung „Shoko Series“ an Serien und Staffeln ein.
    eintraege = jf("/Items?Recursive=true&IncludeItemTypes=Series,Season&Fields=ProviderIds&HasAnyProviderId=Shoko%20Series")["Items"]
    gefunden = set()
    for e in eintraege:
        if (e.get("ProviderIds") or {}).get("Shoko Series") in {str(s) for s in shoko_serien}:
            serie = e.get("SeriesId") or e["Id"]
            if serie not in gefunden:
                gefunden.add(serie)
                jf(f"/Items/{serie}/Refresh?Recursive=true", "POST")
                print(f"Jellyfin liest „{e.get('SeriesName') or e['Name']}“ neu ein")
    if gefunden:
        # Folgen, die trotzdem ohne Shoko-Daten angelegt wurden, noch einmal einzeln einlesen.
        time.sleep(120)
        for serie in gefunden:
            for folge in jf(f"/Shows/{serie}/Episodes?Fields=ProviderIds")["Items"]:
                if folge.get("LocationType") != "Virtual" and "Shoko Episode" not in (folge.get("ProviderIds") or {}):
                    jf(f"/Items/{folge['Id']}/Refresh?MetadataRefreshMode=FullRefresh", "POST")
                    print(f"„{folge['Name']}“ hatte keine Shoko-Daten, wird erneut eingelesen")
        return
    for b in jf("/Library/VirtualFolders"):
        if any("Shokofin/VFS" in ort for ort in b.get("Locations", [])):
            jf(f"/Items/{b['ItemId']}/Refresh?Recursive=true", "POST")
            print(f"Serie in Jellyfin noch unbekannt, „{b['Name']}“ wird neu eingelesen")


def main():
    if not os.path.exists(f"{KONF}/anthropic-key"):
        print(f"Kein Anthropic-Schlüssel in {KONF}/anthropic-key, nichts zu tun")
        return
    os.makedirs(STAND, exist_ok=True)
    stand = lesen(f"{STAND}/stand.json", {})
    offen = lesen(f"{STAND}/offen.json", {})
    jetzt = datetime.now(timezone.utc)
    zugeordnet = set()

    unerkannt = shoko("/api/v3/File?pageSize=200&include_only=Unrecognized&include=AbsolutePaths")["List"]
    ids = {str(d["ID"]) for d in unerkannt}
    # Erledigte oder inzwischen von Shoko selbst erkannte Dateien aus der offenen Liste nehmen.
    for alt in list(offen):
        if alt not in ids:
            del offen[alt]

    gefragt = 0
    try:
        for datei in unerkannt:
            if gefragt >= MAX_PRO_LAUF:
                print(f"Mehr als {MAX_PRO_LAUF} Dateien offen, der Rest kommt im nächsten Lauf")
                break
            gefragt += datei_bearbeiten(datei, stand, offen, zugeordnet, jetzt)
    finally:
        if zugeordnet:
            jellyfin_anstossen(zugeordnet)
        if "--nur-zeigen" not in sys.argv:
            schreiben(f"{STAND}/stand.json", stand)
        schreiben(f"{STAND}/offen.json", offen)


def datei_bearbeiten(datei, stand, offen, zugeordnet, jetzt):
    """Fragt Claude zu einer Datei. Gibt 1 zurück, wenn Claude gefragt wurde, sonst 0."""
    fid = str(datei["ID"])
    erstellt = datetime.fromisoformat(datei["Created"].replace("Z", "+00:00"))
    if jetzt - erstellt < WARTEZEIT:
        return 0
    eintrag = stand.get(fid)
    if isinstance(eintrag, str):  # alter Stand: nur der Zeitpunkt
        eintrag = {"zuletzt": eintrag, "versuche": 1}
    if eintrag and jetzt - datetime.fromisoformat(eintrag["zuletzt"]) < ERNEUT_NACH:
        return 0
    if eintrag and eintrag["versuche"] >= MAX_VERSUCHE:
        return 0  # bleibt in offen.json, bis jemand sie von Hand zuordnet

    pfad = datei["Locations"][0]["RelativePath"] if datei["Locations"] else "?"
    try:
        ergebnis = claude_fragen(datei)
    except anthropic.APIConnectionError as e:
        print(f"{pfad}: Claude nicht erreichbar ({e}), nächster Lauf versucht es erneut")
        return 1
    except anthropic.APIStatusError as e:
        print(f"{pfad}: Claude-Fehler {e.status_code}: {e.message}")
        return 1
    except Exception as e:  # z. B. Shoko kurz weg: diese Datei auslassen, den Lauf nicht abbrechen
        print(f"{pfad}: Fehler {type(e).__name__}: {e}")
        return 1
    stand[fid] = {"zuletzt": jetzt.isoformat(), "versuche": (eintrag or {"versuche": 0})["versuche"] + 1}
    unbekannt = [f for f in ergebnis["folgen_ids"] if f not in gelistete_folgen]
    if unbekannt:
        ergebnis["sicherheit"] = "niedrig"
        ergebnis["begruendung"] += f" [Nicht verknüpft: FolgenIDs {unbekannt} kamen nicht aus folgen_auflisten.]"

    print(f"{pfad}: {ergebnis['sicherheit']} → Folgen {ergebnis['folgen_ids']} – {ergebnis['begruendung']}")
    if ergebnis["sicherheit"] == "hoch" and ergebnis["folgen_ids"] and not NUR_ZEIGEN:
        shoko(f"/api/v3/File/{datei['ID']}/Link", "POST", {"EpisodeIDs": ergebnis["folgen_ids"]})
        print(f"{pfad}: zugeordnet")
        offen.pop(fid, None)
        for folge in ergebnis["folgen_ids"]:
            zugeordnet.add(shoko(f"/api/v3/Episode/{folge}")["IDs"]["ParentSeries"])
    else:
        offen[fid] = {"Pfad": pfad, **ergebnis, "Gefragt": jetzt.isoformat()}
    time.sleep(5)
    return 1


if __name__ == "__main__":
    main()

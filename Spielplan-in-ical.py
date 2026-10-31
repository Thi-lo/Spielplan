#!/usr/bin/env python3
"""
Spielplan-in-ical.py  -  Version 6 (Stand 31.10.2026)

Schauspiel Köln: Vorstellungen als iCal-Feed, gesteuert über einstellungen.toml.

Versionsverlauf
  6  Fehlerbehebung: Termine im Kölender werden zuverlässiger erkannt (Datum und
     Uhrzeit dürfen im HTML in mehrere Teile zerlegt sein; alternativ Tagesüberschrift
     plus Uhrzeit). Wird nichts gefunden, gibt das Protokoll einen Auszug der
     gelesenen Textstücke aus, damit sich die Ursache erkennen lässt.
  5  Vergangene Termine bleiben im Kalender (Einstellung
     vergangene_behalten_monate, Voreinstellung "unbegrenzt").
     Datei umbenannt in Spielplan-in-ical.py; die Versionsnummer erscheint
     beim Start im Protokoll.
  4  Einstellungen in einstellungen.toml ausgelagert (Spielstätten, Stichwörter,
     Zeitraum, Uhrzeit, Wochentage, Kalendername, Ort, Titelaufbau, Standarddauer,
     Notiz). Spielstätte über den Filter des Kölenders (pro Termin statt pro
     Stück). Zusätze wie Premiere oder Tanzgastspiel stehen als Kürzel im Titel
     (z. B. [PREM]) und ausgeschrieben in der Notiz.
  3  Änderungsprotokoll (docs/aenderungen.md und Zusammenfassung des Laufs).
     Termine behalten ihren Zeitstempel, die Datei ändert sich nur bei echten
     Änderungen. Abbruch statt leerem Kalender, wenn keine Termine gefunden werden.
  2  Nur Termine mit genau der Spielstätte "Schauspielhaus" (nicht "Backstage im
     Schauspielhaus"). Link nur noch im Link-Feld statt doppelt. Kein Ort im Termin.
  1  Erste Fassung: Kölender durchblättern, Produktionsseiten lesen (Spielstätte,
     Dauer, Termine), Schauspielhaus filtern, .ics schreiben.

Ablauf:
 1. Für jede gewünschte Spielstätte den Kölender mit deren Filter durchblättern
    (kalender?f[0]=activity_stage_kalender:ID&page=N). Pro Termin stehen dort
    Zeit, Zusatz (z. B. Premiere), Titel, Link und Hinweise.
 2. Nach den Einstellungen filtern (Stichwörter, Zeitraum, Uhrzeit, Wochentage).
 3. Für die übrig gebliebenen Produktionen die Dauer von der Produktionsseite lesen.
 4. Vergangene Termine aus der bisherigen Datei übernehmen.
 5. .ics schreiben und Änderungen zum letzten Lauf protokollieren.

Installation:  pip3 install requests beautifulsoup4     (Python 3.11 oder neuer)
Einmalig:      python3 Spielplan-in-ical.py --out schauspielhaus.ics
Spielstätten:  python3 Spielplan-in-ical.py --spielstaetten
Debug:         python3 Spielplan-in-ical.py --debug
Als Feed:      python3 Spielplan-in-ical.py --serve 8000
"""
import argparse
import calendar
import copy
import hashlib
import os
import re
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, Comment

try:
    import tomllib
except ModuleNotFoundError:      # Python < 3.11
    tomllib = None

# ----------------------------------------------------------------------
VERSION = "6"
BASE = "https://www.schauspiel.koeln"
CALENDAR_URL = BASE + "/kalender"
TZ = ZoneInfo("Europe/Berlin")
MAX_PAGES = 30
PAUSE_BETWEEN_REQUESTS = 0.3             # Sekunden, bitte nicht kleiner machen
UID_SUFFIX = "@schauspiel-koeln-ical"
LOG_HEADER = "# Änderungen am Kalender\n\nNeueste Einträge stehen oben.\n\n"
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

# Standardwerte; einstellungen.toml überschreibt sie
DEFAULTS = {
    "auswahl": {
        "spielstaetten": ["Schauspielhaus"],
        "stichwoerter_einschliessen": [],
        "stichwoerter_ausschliessen": [],
        "von_datum": "",
        "bis_datum": "",
        "naechste_monate": 0,
        "fruehestens_uhrzeit": "",
        "spaetestens_uhrzeit": "",
        "wochentage": [],
        "vergangene_behalten_monate": "unbegrenzt",
    },
    "termin": {
        "kalendername": "Schauspiel Köln – Schauspielhaus",
        "ort": "",
        "titel_format": "{kuerzel} {titel}",
        "standarddauer_minuten": 120,
        "dauer_von_produktionsseite": True,
        "unbekannter_zusatz_max_zeichen": 8,
    },
    "notiz": {"zusatz": True, "hinweis": True, "dauer": True, "pause": True},
    "kuerzel": {
        "Premiere": "PREM", "Köln-Premiere": "KÖLN-PREM", "Uraufführung": "UA",
        "Wiederaufnahme": "WA", "Tanzgastspiel": "TANZ", "Gastspiel": "GAST",
        "Lesung": "LESUNG", "Konzert": "KONZERT", "Führung": "FÜHR",
        "Party": "PARTY", "Workshop": "WORKSHOP",
    },
}


def load_settings(path):
    cfg = copy.deepcopy(DEFAULTS)
    if not path or not os.path.exists(path):
        print("Keine Einstellungsdatei gefunden - Standardwerte werden verwendet.")
        return cfg
    if tomllib is None:
        raise SystemExit("Für die Einstellungsdatei wird Python 3.11 oder neuer benötigt.")
    try:
        with open(path, "rb") as f:
            user = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"Fehler in {path}: {e}")
    for section, values in user.items():
        if section not in cfg:
            print(f"Hinweis: Abschnitt [{section}] ist unbekannt und wird ignoriert.")
            continue
        for key, value in values.items():
            if section != "kuerzel" and key not in cfg[section]:
                print(f"Hinweis: Einstellung '{key}' in [{section}] ist unbekannt.")
                continue
            cfg[section][key] = value
    return cfg


# ----------------------------- Abruf -----------------------------------
session = requests.Session()
session.headers["User-Agent"] = "Mozilla/5.0 (private iCal-Feed, eigener Gebrauch)"


def get(url, params=None):
    time.sleep(PAUSE_BETWEEN_REQUESTS)
    r = session.get(url, params=params, timeout=25)
    r.raise_for_status()
    return r.text


def venue_ids():
    """Spielstätten-Namen -> Filter-ID, aus dem Filterbereich des Kölenders."""
    soup = BeautifulSoup(get(CALENDAR_URL), "html.parser")
    ids = {}
    for a in soup.find_all("a", href=True):
        m = re.search(r"activity_stage_kalender(?:%3A|:)(\d+)", a["href"])
        if not m:
            continue
        name = re.sub(r"^\(-\)\s*", "", a.get_text(" ", strip=True)).rstrip("|").strip()
        if name and name not in ids:
            ids[name] = m.group(1)
    return ids


MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12, "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "mai": 5,
    "juni": 6, "juli": 7, "oktober": 10, "dezember": 12,
}
EVENT_DT_RE = re.compile(
    r"^[^\W\d_]+,\s*(\d{1,2})\.?\s+([^\W\d_]+)\.?\s+(\d{4})\s+(\d{1,2})[.:](\d{2})\s*Uhr")
HEADING_RE = re.compile(r"^[^\W\d_]+,\s*\d{1,2}\.\d{1,2}\.\d{2,4}$")
STATUS_RE = re.compile(r"^(ausverkauft|sold out)$", re.I)
STOP_TEXTS = ("Zur vorherigen Seite", "BKO Schauspiel Footer")


BLOCK_TAGS = ["div", "li", "p", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
              "section", "article", "header", "footer", "main", "table", "tr",
              "td", "th", "dl", "dt", "dd", "figure", "figcaption", "blockquote"]
SEP = "\u2063"    # unsichtbares Trennzeichen als Ersatz für <br>
TIME_ONLY_RE = re.compile(r"^(\d{1,2})[.:](\d{2})\s*Uhr$")
HEADING_PARTS_RE = re.compile(r"^[^\W\d_]+,\s*(\d{1,2})\.(\d{1,2})\.(\d{2,4})$")


def iter_tokens(html):
    """Textstücke der Seite als Liste von (text, href). Text, der im HTML in
    mehrere Teile zerlegt ist (z. B. <time>Datum</time> <span>Uhrzeit</span>),
    wird zu einem Stück verbunden, solange er im selben Block und im selben
    Link steht."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    for br in soup.find_all("br"):
        br.replace_with(SEP)

    tokens, key, parts, href = [], None, [], None
    for s in soup.find_all(string=True):
        if isinstance(s, Comment):
            continue
        raw = str(s)
        if raw == SEP:
            if parts:
                tokens.append((" ".join(parts), href))
            parts, key = [], None
            continue
        text = " ".join(raw.split())
        if not text:
            continue
        a = s.find_parent("a")
        k = (id(s.find_parent(BLOCK_TAGS)), id(a) if a else None)
        if k != key:
            if parts:
                tokens.append((" ".join(parts), href))
            parts, key = [], k
            href = a.get("href") if a else None
        parts.append(text)
    if parts:
        tokens.append((" ".join(parts), href))
    return tokens


def token_sample(html, n=60):
    """Auszug der gelesenen Textstücke rund um die erste Uhrzeit (zur Fehlersuche)."""
    toks = iter_tokens(html)
    idx = next((i for i, (t, _) in enumerate(toks)
                if re.search(r"\d{1,2}[.:]\d{2}\s*Uhr", t)), 0)
    first = max(0, idx - 4)
    return [f"{i:4} {'[Link] ' if h else ''}{t[:90]}"
            for i, (t, h) in enumerate(toks[first:first + n], first)]


def parse_listing(html):
    """Termine einer Kölender-Seite. Reihenfolge je Termin im Text:
    Datum/Zeit, [Zusatz], Titel (Link auf /produktion/), [Hinweise], Ticket-Link.
    Die Zeit steht als "Saturday, 31 October 2026 19.30 Uhr"; fehlt diese Zeile,
    dienen die Tagesüberschrift ("Samstag, 31.10.26") und eine Zeile "19.30 Uhr"."""
    events, cur, closed, heading = [], None, True, None
    for text, href in iter_tokens(html):
        if text.startswith(STOP_TEXTS):
            break

        start, rest = None, ""
        if not href:
            m = EVENT_DT_RE.match(text)
            try:
                if m and MONTHS.get(m.group(2).casefold()):
                    day, _, year, hh, mm = m.groups()
                    start = datetime(int(year), MONTHS[m.group(2).casefold()], int(day),
                                     int(hh), int(mm), tzinfo=TZ)
                    rest = text[m.end():].strip(" |")
                else:
                    m = TIME_ONLY_RE.match(text)
                    if m and heading and (cur is None or closed):
                        start = datetime(*heading, int(m.group(1)), int(m.group(2)), tzinfo=TZ)
            except ValueError:
                start = None
        if start:
            cur = {"start": start, "tags": [rest] if rest else [], "title_parts": [],
                   "href": None, "notes": [], "status": ""}
            events.append(cur)
            closed = False
            continue

        h = None if href else HEADING_PARTS_RE.match(text)
        if h:
            d, mo, y = (int(x) for x in h.groups())
            heading = (y + 2000 if y < 100 else y, mo, d)
            closed = True
            continue
        if cur is None or closed:
            continue
        if href:
            if "/produktion/" in href and cur["href"] in (None, href):
                cur["href"] = href
                cur["title_parts"].append(text)
            else:                    # Ticket-/Shop-Link = Ende dieses Termins
                closed = True
            continue
        if STATUS_RE.match(text):
            cur["status"] = text
        elif cur["href"] is None:
            cur["tags"].append(text)
        else:
            cur["notes"].append(text)

    result = []
    for e in events:
        if not e["href"] or not e["title_parts"]:
            continue
        url = urljoin(BASE, e["href"]).split("#")[0].split("?")[0]
        result.append({
            "start": e["start"], "tags": e["tags"], "title": " ".join(e["title_parts"]),
            "url": url, "notes": e["notes"], "status": e["status"],
            "venue": "", "duration": None, "pause": "",
        })
    return result


DURATION_RE = re.compile(r"^(?:(\d+)\s*h)?\s*(?:(\d+)\s*min)?$", re.I)


def parse_duration(line):
    m = DURATION_RE.match(line.strip())
    if not m or not (m.group(1) or m.group(2)):
        return None
    return timedelta(hours=int(m.group(1) or 0), minutes=int(m.group(2) or 0))


def production_details(html):
    """Dauer und Pausenhinweis von der Produktionsseite."""
    soup = BeautifulSoup(html, "html.parser")
    lines = [l.strip() for l in soup.get_text("\n").split("\n") if l.strip()]
    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else ""
    try:
        term_idx = next(i for i, l in enumerate(lines) if l == "Termine")
        title_idx = max(i for i, l in enumerate(lines[:term_idx]) if l == title)
    except (ValueError, StopIteration):
        title_idx = next((i for i, l in enumerate(lines) if l == title), 0)
    window = lines[title_idx: title_idx + 12]
    for i, l in enumerate(window):
        d = parse_duration(l)
        if d:
            pause = window[i + 1] if i + 1 < len(window) and "pause" in window[i + 1].lower() else ""
            return d, pause
    return None, ""


# --------------------------- Auswahl -----------------------------------
def add_months(dt, n):
    y, m = divmod(dt.month - 1 + n, 12)
    year, month = dt.year + y, m + 1
    return dt.replace(year=year, month=month,
                      day=min(dt.day, calendar.monthrange(year, month)[1]))


def parse_day(s):
    return datetime.fromisoformat(s).replace(tzinfo=TZ)


def parse_clock(s):
    return datetime.strptime(s, "%H:%M").time()


def weekday_numbers(names):
    nums = set()
    for n in names:
        key = n.strip().casefold()[:2]
        if key not in [w.casefold() for w in WEEKDAYS]:
            raise SystemExit(f"Unbekannter Wochentag '{n}' - erlaubt: {', '.join(WEEKDAYS)}")
        nums.add([w.casefold() for w in WEEKDAYS].index(key))
    return nums


def keep(e, a, now):
    if e["start"] < now - timedelta(hours=3):
        return False
    inc = [k.casefold() for k in a["stichwoerter_einschliessen"]]
    if inc and not any(k in e["title"].casefold() for k in inc):
        return False
    haystack = " ".join([e["title"], *e["tags"], *e["notes"]]).casefold()
    if any(k.casefold() in haystack for k in a["stichwoerter_ausschliessen"]):
        return False
    if a["von_datum"] and e["start"] < parse_day(a["von_datum"]):
        return False
    if a["bis_datum"] and e["start"] >= parse_day(a["bis_datum"]) + timedelta(days=1):
        return False
    if a["naechste_monate"] and e["start"] > add_months(now, int(a["naechste_monate"])):
        return False
    t = e["start"].time()
    if a["fruehestens_uhrzeit"] and t < parse_clock(a["fruehestens_uhrzeit"]):
        return False
    if a["spaetestens_uhrzeit"] and t > parse_clock(a["spaetestens_uhrzeit"]):
        return False
    if a["wochentage"] and e["start"].weekday() not in weekday_numbers(a["wochentage"]):
        return False
    return True


def collect(cfg):
    """Termine laden, filtern und mit Dauer versehen."""
    a = cfg["auswahl"]
    now = datetime.now(TZ)
    wanted = []
    if a["spielstaetten"]:
        ids = venue_ids()
        lower = {n.casefold(): n for n in ids}
        for name in a["spielstaetten"]:
            real = lower.get(name.strip().casefold())
            if not real:
                raise SystemExit(f"Spielstätte '{name}' gibt es nicht. Verfügbar: "
                                 + ", ".join(sorted(ids)))
            wanted.append((real, ids[real]))
    else:
        wanted.append(("", None))          # keine Einschränkung

    found, first_html = {}, None
    for venue, vid in wanted:
        for page in range(MAX_PAGES):
            params = {"page": page}
            if vid:
                params["f[0]"] = f"activity_stage_kalender:{vid}"
            new = 0
            html = get(CALENDAR_URL, params)
            if first_html is None:
                first_html = html
            for e in parse_listing(html):
                key = (e["url"], e["start"])
                if key not in found:
                    e["venue"] = venue
                    found[key] = e
                    new += 1
            if new == 0:
                break
    if not found:
        print(f"Erste Kölender-Seite: {len(first_html or '')} Zeichen, "
              f"{(first_html or '').count('/produktion/')} Verweise auf /produktion/. "
              "Auszug der gelesenen Textstücke:")
        for line in token_sample(first_html or ""):
            print(line)
        raise SystemExit("Keine Termine gefunden - Datei bleibt unverändert.")

    events = [e for e in found.values() if keep(e, a, now)]

    if cfg["termin"]["dauer_von_produktionsseite"]:
        cache = {}
        for e in events:
            if e["url"] not in cache:
                try:
                    cache[e["url"]] = production_details(get(e["url"]))
                except Exception as ex:
                    print(f"Dauer nicht lesbar für {e['url']}: {ex}")
                    cache[e["url"]] = (None, "")
            e["duration"], e["pause"] = cache[e["url"]]
    return events


# ---------------------------- iCal -------------------------------------
def esc(s):
    return (s.replace("\\", "\\\\").replace(";", "\\;")
             .replace(",", "\\,").replace("\n", "\\n"))


def fold(line):
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode("utf-8")) > 74:
            out.append(cur)
            cur = " " + ch
        else:
            cur += ch
    out.append(cur)
    return "\r\n".join(out)


def utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def fmt_duration(d):
    h, m = divmod(int(d.total_seconds()) // 60, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def kuerzel_for(tag, cfg):
    table = {k.casefold(): v for k, v in cfg["kuerzel"].items()}
    if tag.casefold() in table:
        return table[tag.casefold()]
    return tag.upper()[: int(cfg["termin"]["unbekannter_zusatz_max_zeichen"])]


def make_title(e, cfg):
    kz = "".join(f"[{k}]" for k in (kuerzel_for(t, cfg) for t in e["tags"]) if k)
    try:
        s = cfg["termin"]["titel_format"].format(
            kuerzel=kz, titel=e["title"], zusatz=", ".join(e["tags"]))
    except KeyError as ex:
        raise SystemExit(f"titel_format enthält unbekannten Platzhalter {ex}. "
                         "Erlaubt: {kuerzel} {titel} {zusatz}")
    return " ".join(s.split())


def make_description(e, cfg):
    n = cfg["notiz"]
    lines = []
    if n["zusatz"] and e["tags"]:
        lines.append("Zusatz: " + ", ".join(e["tags"]))
    if n["hinweis"]:
        lines += e["notes"]
    if n["dauer"] and e["duration"]:
        lines.append(f"Dauer: {fmt_duration(e['duration'])}")
    if n["pause"] and e["pause"]:
        lines.append(e["pause"])
    return "\n".join(lines)


def build_ics(listing, cfg, old=None):
    """Erzeugt (ics_text, events). 'old' = frühere Termine, damit unveränderte
    Termine ihren DTSTAMP behalten und die Datei sich nur bei echten
    Änderungen ändert."""
    now = datetime.now(TZ)
    old = old or {}
    default_dur = timedelta(minutes=int(cfg["termin"]["standarddauer_minuten"]))
    events = {}
    for e in listing:
        uid = hashlib.sha1(f"{e['url']}{e['start'].isoformat()}".encode()).hexdigest() + UID_SUFFIX
        events[uid] = {
            "title": make_title(e, cfg), "url": e["url"], "start": e["start"],
            "end": e["start"] + (e["duration"] or default_dur),
            "desc": make_description(e, cfg),
            "location": " ".join(cfg["termin"]["ort"].format(spielstaette=e["venue"]).split()),
            "stamp": old[uid]["stamp"] if uid in old else utc(now),
        }

    # Vergangene Termine aus der bisherigen Datei übernehmen (Einstellung
    # vergangene_behalten_monate: "unbegrenzt", Anzahl Monate oder 0 = nicht behalten)
    keep_past = cfg["auswahl"]["vergangene_behalten_monate"]
    if isinstance(keep_past, str):
        if keep_past.strip().casefold() in ("unbegrenzt", "immer"):
            keep_past = None
        else:
            try:
                keep_past = int(keep_past)
            except ValueError:
                raise SystemExit("vergangene_behalten_monate: erlaubt sind "
                                 '"unbegrenzt" oder eine Zahl (0 = nicht behalten).')
    horizon = now - timedelta(hours=3)
    for uid, o in old.items():
        if uid in events or o["start"] >= horizon:
            continue   # aktuelle/zukünftige Termine kommen nur vom Theater
        if keep_past is None or (keep_past > 0 and o["start"] >= add_months(now, -keep_past)):
            events[uid] = dict(o)

    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//schauspiel-koeln-ical//DE",
             "CALSCALE:GREGORIAN", f"X-WR-CALNAME:{esc(cfg['termin']['kalendername'])}",
             "X-WR-TIMEZONE:Europe/Berlin"]
    for uid, e in sorted(events.items(), key=lambda kv: (kv[1]["start"], kv[0])):
        lines += ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{e['stamp']}",
                  f"DTSTART:{utc(e['start'])}", f"DTEND:{utc(e['end'])}",
                  f"SUMMARY:{esc(e['title'])}"]
        if e["location"]:
            lines.append(f"LOCATION:{esc(e['location'])}")
        if e["desc"]:
            lines.append(f"DESCRIPTION:{esc(e['desc'])}")
        lines += [f"URL:{e['url']}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n", events


# ------------------------ Änderungsprotokoll ---------------------------
def unesc(v):
    return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) in "nN" else m.group(1), v)


def parse_utc(v):
    return (datetime.strptime(v, "%Y%m%dT%H%M%SZ")
            .replace(tzinfo=timezone.utc).astimezone(TZ))


def read_ics(path):
    """Liest eine vorhandene .ics-Datei -> {uid: Termin} oder None."""
    try:
        with open(path, encoding="utf-8") as f:
            return parse_ics(f.read())
    except FileNotFoundError:
        return None


def parse_ics(text):
    text = text.replace("\r\n ", "").replace("\n ", "")   # gefaltete Zeilen
    events = {}
    for block in text.split("BEGIN:VEVENT")[1:]:
        fields = {}
        for line in block.split("END:VEVENT")[0].splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                fields[k.split(";")[0]] = v
        try:
            events[fields["UID"]] = {
                "title": unesc(fields.get("SUMMARY", "")), "url": fields.get("URL", ""),
                "stamp": fields["DTSTAMP"],
                "start": parse_utc(fields["DTSTART"]), "end": parse_utc(fields["DTEND"]),
                "desc": unesc(fields.get("DESCRIPTION", "")),
                "location": unesc(fields.get("LOCATION", "")),
            }
        except (KeyError, ValueError):
            continue
    return events


def fmt_dt(dt):
    return f"{WEEKDAYS[dt.weekday()]} {dt:%d.%m.%Y}, {dt:%H:%M} Uhr"


def md(s):
    return s.replace("[", "\\[").replace("]", "\\]")


def make_report(old, new, now):
    """Lesbares Protokoll (Markdown); leer, wenn sich nichts geändert hat."""
    horizon = now - timedelta(hours=3)   # Vergangenes zählt nicht als "entfallen"
    key = lambda e: e["start"]
    added = sorted((new[u] for u in new if u not in old), key=key)
    removed = sorted((old[u] for u in old
                      if u not in new and old[u]["start"] >= horizon), key=key)
    changed = []
    for u in new:
        if u not in old:
            continue
        o, n = old[u], new[u]
        parts = []
        if o["title"] != n["title"]:
            parts.append(f"Titel vorher „{o['title']}“")
        if o["end"] != n["end"]:
            parts.append(f"Ende jetzt {n['end']:%H:%M} Uhr (vorher {o['end']:%H:%M} Uhr)")
        if parts:
            changed.append((n, "; ".join(parts)))
    changed.sort(key=lambda t: t[0]["start"])
    if not (added or removed or changed):
        return ""

    def name(e):
        return f"[{md(e['title'])}]({e['url']})" if e.get("url") else md(e["title"])

    now_s = f"{now:%d.%m.%Y}"
    out = [f"## {now_s} – {len(added)} neu, {len(removed)} nicht mehr "
           f"enthalten, {len(changed)} geändert\n"]
    if added:
        out += ["**Neu**\n"] + [f"- {name(e)}: {fmt_dt(e['start'])}" for e in added] + [""]
    if removed:
        out += ["**Nicht mehr enthalten (abgesagt oder verschoben)**\n"]
        out += [f"- {name(e)}: {fmt_dt(e['start'])}" for e in removed] + [""]
    if changed:
        out += ["**Geändert**\n"]
        out += [f"- {name(n)}: {fmt_dt(n['start'])} – {txt}" for n, txt in changed] + [""]
    return "\n".join(out)


def prepend_log(path, report):
    body = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            txt = f.read()
        body = txt[len(LOG_HEADER):] if txt.startswith(LOG_HEADER) else txt
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(LOG_HEADER + report + "\n" + body)


# ---------------------------- CLI --------------------------------------
def serve(port, cfg, max_age=6 * 3600):
    cache = {"ics": "", "ts": 0.0}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] != "/schauspielhaus.ics":
                self.send_error(404)
                return
            if time.time() - cache["ts"] > max_age:
                try:
                    old = parse_ics(cache["ics"]) if cache["ics"] else None
                    cache["ics"], ev = build_ics(collect(cfg), cfg, old)
                    cache["ts"] = time.time()
                    print(f"Aktualisiert: {len(ev)} Termine")
                except Exception as e:
                    print("Fehler:", e)
                    if not cache["ics"]:
                        self.send_error(502)
                        return
            body = cache["ics"].encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/calendar; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    print(f"Feed: http://0.0.0.0:{port}/schauspielhaus.ics")
    HTTPServer(("0.0.0.0", port), H).serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", action="version", version=f"Spielplan-in-ical Version {VERSION}")
    ap.add_argument("--config", default="einstellungen.toml")
    ap.add_argument("--out", default="schauspielhaus.ics")
    ap.add_argument("--log", metavar="DATEI",
                    help="Änderungsprotokoll (Markdown) hier fortschreiben")
    ap.add_argument("--serve", type=int, metavar="PORT")
    ap.add_argument("--spielstaetten", action="store_true",
                    help="verfügbare Spielstätten anzeigen und beenden")
    ap.add_argument("--debug", action="store_true", help="alle gefundenen Termine ausgeben")
    args = ap.parse_args()

    if args.spielstaetten:
        for name in sorted(venue_ids()):
            print(name)
        raise SystemExit(0)

    print(f"Spielplan-in-ical Version {VERSION}")
    cfg = load_settings(args.config)
    if args.serve:
        serve(args.serve, cfg)
    else:
        listing = collect(cfg)
        if args.debug:
            for e in listing:
                print(f"{e['start']:%d.%m.%Y %H:%M} | {','.join(e['tags']) or '-':14} | "
                      f"{e['title'][:40]:40} | dauer={e['duration']} | "
                      f"hinweis={' / '.join(e['notes'])[:50]}")
        old = read_ics(args.out)
        ics, events = build_ics(listing, cfg, old)
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8", newline="") as f:
            f.write(ics)
        print(f"{len(events)} Termine -> {args.out}")

        if old is None:
            print("Erster Lauf - kein Vergleich möglich.")
        else:
            report = make_report(old, events, datetime.now(TZ))
            if not report:
                print("Keine Änderungen seit dem letzten Lauf.")
            else:
                print("\n" + report)
                if args.log:
                    prepend_log(args.log, report)
                summary = os.environ.get("GITHUB_STEP_SUMMARY")
                if summary:
                    with open(summary, "a", encoding="utf-8") as f:
                        f.write(report + "\n")

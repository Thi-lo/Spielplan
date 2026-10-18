#!/usr/bin/env python3
"""
Schauspiel Köln: Vorstellungen im Schauspielhaus als iCal-Feed.
 
Ablauf:
 1. Kölender (https://www.schauspiel.koeln/kalender?page=N) durchblättern
    und alle Links auf /produktion/... einsammeln.
 2. Jede Produktionsseite öffnen: Spielstätte, Dauer und alle Termine lesen.
 3. Nur Produktionen im Schauspielhaus behalten, daraus .ics erzeugen.
 
Installation:  pip install requests beautifulsoup4
Einmalig:      python schauspiel_koeln_ical.py --out schauspielhaus.ics
Debug:         python schauspiel_koeln_ical.py --debug   (zeigt Spielstätte je Produktion)
Als Feed:      python schauspiel_koeln_ical.py --serve 8000
               -> Kalender abonnieren: http://<rechner>:8000/schauspielhaus.ics
"""
import argparse
import hashlib
import re
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urljoin
from zoneinfo import ZoneInfo
 
import requests
from bs4 import BeautifulSoup
 
# ----------------------------------------------------------------------
BASE = "https://www.schauspiel.koeln"
CALENDAR_URL = BASE + "/kalender"
TARGET_VENUE = "Schauspielhaus"          # andere: "Kleines Haus", "Depot"
LOCATION = ""   # leer = kein Ort im Termin; sonst z. B. "Offenbachplatz, 50667 Köln"
TZ = ZoneInfo("Europe/Berlin")
DEFAULT_DURATION = timedelta(hours=2)    # falls keine Dauer angegeben ist
EXCLUDE_TITLE_KEYWORDS = []              # z. B. ["Theaterführung"]
EXCLUDE_NOTE_KEYWORDS = []               # z. B. ["Schulvorstellung"]
MAX_PAGES = 30
PAUSE_BETWEEN_REQUESTS = 0.3             # Sekunden, bitte nicht kleiner machen
# ----------------------------------------------------------------------
 
session = requests.Session()
session.headers["User-Agent"] = "Mozilla/5.0 (private iCal-Feed, kontakt: eigener Gebrauch)"
 
DURATION_RE = re.compile(r"^(?:(\d+)\s*h)?\s*(?:(\d+)\s*min)?$", re.I)
DATE_RE = re.compile(
    r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})\s+(\d{1,2})[.:](\d{2})\s*Uhr"
)
 
 
def get(url):
    time.sleep(PAUSE_BETWEEN_REQUESTS)
    r = session.get(url, timeout=25)
    r.raise_for_status()
    return r.text
 
 
def production_links():
    """Alle Produktions-URLs aus dem paginierten Kölender."""
    seen = {}
    for page in range(MAX_PAGES):
        soup = BeautifulSoup(get(f"{CALENDAR_URL}?page={page}"), "html.parser")
        new = 0
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/produktion/" not in href or "/en/" in href:
                continue
            url = urljoin(BASE, href.split("#")[0].split("?")[0])
            if url not in seen:
                seen[url] = a.get_text(" ", strip=True)
                new += 1
        if new == 0:  # leere oder bereits bekannte Seite -> Ende
            break
    return list(seen)
 
 
def parse_duration(line):
    m = DURATION_RE.match(line.strip())
    if not m or not (m.group(1) or m.group(2)):
        return None
    return timedelta(hours=int(m.group(1) or 0), minutes=int(m.group(2) or 0))
 
 
def parse_production(html, url):
    soup = BeautifulSoup(html, "html.parser")
    lines = [l.strip() for l in soup.get_text("\n").split("\n") if l.strip()]
 
    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else url.rsplit("/", 1)[-1]
    try:
        title_idx = max(i for i, l in enumerate(lines) if l == title)
        # letztes Vorkommen vor "Termine" ist die Seitenüberschrift
        term_idx = next(i for i, l in enumerate(lines) if l == "Termine")
        title_idx = max(i for i, l in enumerate(lines[:term_idx]) if l == title)
    except (ValueError, StopIteration):
        title_idx, term_idx = 0, None
 
    # Spielstätte, Dauer, Pause: Zeilen zwischen Titel und Beschreibung
    venue_lines, duration, pause = [], None, ""
    window = lines[title_idx: title_idx + 12]
    for i, l in enumerate(window):
        d = parse_duration(l)
        if d:
            duration = d
            venue_lines = window[max(0, i - 2): i]
            if i + 1 < len(window) and "pause" in window[i + 1].lower():
                pause = window[i + 1]
            break
    if not duration:
        venue_lines = window
 
    # Termine: Text ab der Überschrift "Termine"
    dates = []
    if term_idx is not None:
        block = " ".join(lines[term_idx + 1:])
        for stop in ("Weitere Termine", "Mit externem Anbieter"):
            block = block.split(stop)[0]
        for m in DATE_RE.finditer(block):
            day, mon, yr, hh, mm = (int(x) for x in m.groups())
            yr += 2000 if yr < 100 else 0
            dates.append(datetime(yr, mon, day, hh, mm, tzinfo=TZ))
 
    return {
        "title": title, "url": url, "venue_lines": venue_lines,
        "venue_text": " | ".join(venue_lines),
        "duration": duration, "pause": pause, "dates": dates,
    }
 
 
def collect():
    productions = []
    for url in production_links():
        try:
            productions.append(parse_production(get(url), url))
        except Exception as e:
            print(f"Übersprungen {url}: {e}")
    return productions
 
 
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
 
 
def build_ics(productions):
    now = datetime.now(TZ)
    stamp = utc(now)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//schauspiel-koeln-ical//DE",
             "CALSCALE:GREGORIAN", f"X-WR-CALNAME:Schauspiel Köln – {TARGET_VENUE}",
             "X-WR-TIMEZONE:Europe/Berlin"]
    count = 0
    for p in productions:
        # nur, wenn eine Zeile genau der Spielstätte entspricht
        # (also nicht "Backstage im Schauspielhaus" o. ä.)
        if not any(l.strip().casefold() == TARGET_VENUE.casefold()
                   for l in p["venue_lines"]):
            continue
        if any(k.lower() in p["title"].lower() for k in EXCLUDE_TITLE_KEYWORDS):
            continue
        dur = p["duration"] or DEFAULT_DURATION
        desc = [f"Dauer: {fmt_duration(p['duration'])}" if p["duration"] else "",
                p["pause"]]
        for start in p["dates"]:
            if start < now - timedelta(hours=3):
                continue
            uid = hashlib.sha1(f"{p['url']}{start.isoformat()}".encode()).hexdigest()
            lines += [
                "BEGIN:VEVENT", f"UID:{uid}@schauspiel-koeln-ical", f"DTSTAMP:{stamp}",
                f"DTSTART:{utc(start)}", f"DTEND:{utc(start + dur)}",
                f"SUMMARY:{esc(p['title'])}",
            ]
            if LOCATION:
                lines.append(f"LOCATION:{esc(LOCATION)}")
            lines += [
                f"DESCRIPTION:{esc(chr(10).join(x for x in desc if x))}",
                f"URL:{p['url']}", "END:VEVENT",
            ]
            count += 1
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n", count
 
 
# ---------------------------- CLI --------------------------------------
def serve(port, max_age=6 * 3600):
    cache = {"ics": "", "ts": 0.0}
 
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] != "/schauspielhaus.ics":
                self.send_error(404)
                return
            if time.time() - cache["ts"] > max_age:
                try:
                    cache["ics"], n = build_ics(collect())
                    cache["ts"] = time.time()
                    print(f"Aktualisiert: {n} Termine")
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
    ap.add_argument("--out", default="schauspielhaus.ics")
    ap.add_argument("--serve", type=int, metavar="PORT")
    ap.add_argument("--debug", action="store_true",
                    help="Spielstätte/Dauer/Terminzahl je Produktion ausgeben")
    args = ap.parse_args()
 
    if args.serve:
        serve(args.serve)
    else:
        prods = collect()
        if args.debug:
            for p in prods:
                print(f"{p['title']!r:55} venue={p['venue_text']!r:40} "
                      f"dauer={p['duration']} termine={len(p['dates'])}")
        ics, n = build_ics(prods)
        with open(args.out, "w", encoding="utf-8", newline="") as f:
            f.write(ics)
        print(f"{n} Termine im {TARGET_VENUE} -> {args.out}")
 

"""Parst text/calendar (.ics) Parts zu einem schlanken Event-Dict für die
Detail-Panel-Vorschau in Mailflow.

Kein externer Dependency — manueller VEVENT-Parser, robust gegen RFC-5545-
Line-Folding, Escapes (\\n, \\, , \\;) und Windows-Zeitzonen. Liefert nur die
fürs UI nötigen Felder; extrahiert insbesondere den Online-Meeting-Join-Link
(Teams/Zoom/Meet) und filtert Microsoft-Störlinks (meetingOptions, aka.ms) raus.
"""
import email as _email_stdlib
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
    _BERLIN = ZoneInfo("Europe/Berlin")
except Exception:  # pragma: no cover
    _BERLIN = None

_WEEKDAYS = ["Mo.", "Di.", "Mi.", "Do.", "Fr.", "Sa.", "So."]

_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
# Echte Join-Links erkennen …
_JOIN_GOOD = re.compile(
    r"(teams\.microsoft\.com/(l/meetup-join|meet)/|zoom\.us/j/|"
    r"meet\.google\.com/|whereby\.com/|\.webex\.com/)", re.I)
# … und Microsoft-Beiwerk ausschließen (Optionen-Seite, Hilfe-Shortlink, Schemas).
_JOIN_BAD = re.compile(r"(meetingOptions|aka\.ms|schemas\.microsoft\.com|w3\.org)", re.I)


def _unfold(text: str) -> str:
    """RFC-5545: Fortsetzungszeilen beginnen mit Space/Tab → an Vorzeile anhängen."""
    return re.sub(r"\r?\n[ \t]", "", text)


def _unescape(v: str) -> str:
    return (v.replace("\\N", "\n").replace("\\n", "\n")
             .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")).strip()


def _find_calendar_text(raw_bytes: bytes) -> str | None:
    try:
        msg = _email_stdlib.message_from_bytes(raw_bytes)
    except Exception:
        return None
    for part in msg.walk():
        if part.get_content_type() == "text/calendar":
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            for enc in (charset, "utf-8", "latin-1"):
                try:
                    return payload.decode(enc, errors="replace")
                except (LookupError, UnicodeDecodeError):
                    continue
    return None


def _parse_props(lines):
    """Gibt (props, attendees). props[NAME] = (params_dict, value); letzter
    Wert gewinnt. ATTENDEE wird gesammelt."""
    props, attendees = {}, []
    for line in lines:
        if ":" not in line:
            continue
        head, value = line.split(":", 1)
        segs = head.split(";")
        name = segs[0].upper()
        params = {}
        for p in segs[1:]:
            if "=" in p:
                k, val = p.split("=", 1)
                params[k.upper()] = val.strip('"')
        if name == "ATTENDEE":
            attendees.append((params, value))
        else:
            props[name] = (params, value)
    return props, attendees


def _parse_dt(params, value):
    """Gibt (datetime|None, all_day). Behandelt Z (UTC→Berlin), TZID (Wandzeit)
    und reine DATE-Werte. Windows-TZIDs werden als lokale Wandzeit angezeigt —
    für Stefans Zeitzone (W. Europe ≈ Europe/Berlin) korrekt, ohne vollständige
    Windows→IANA-Map."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        try:
            return datetime.strptime(value[:8], "%Y%m%d"), True
        except ValueError:
            return None, True
    m = re.match(r"(\d{8}T\d{6})(Z?)", value)
    if not m:
        return None, False
    dt = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S")
    if m.group(2) == "Z":
        dt = dt.replace(tzinfo=timezone.utc)
        if _BERLIN:
            dt = dt.astimezone(_BERLIN)
    return dt, False


def _fmt_when(start, end, all_day) -> str:
    if not start:
        return ""
    wd, d = _WEEKDAYS[start.weekday()], start.strftime("%d.%m.%Y")
    if all_day:
        return f"{wd} {d} · ganztägig"
    s = start.strftime("%H:%M")
    if end and end.date() == start.date():
        return f"{wd} {d} · {s}–{end.strftime('%H:%M')} Uhr"
    if end:
        wd2 = _WEEKDAYS[end.weekday()]
        return f"{wd} {d} {s} – {wd2} {end.strftime('%d.%m.%Y %H:%M')} Uhr"
    return f"{wd} {d} · {s} Uhr"


def _extract_join(*texts) -> str:
    for t in texts:
        if not t:
            continue
        for url in _URL_RE.findall(t):
            url = url.rstrip(">.,;)")
            if _JOIN_GOOD.search(url) and not _JOIN_BAD.search(url):
                return url
    return ""


def extract_calendar_event(raw_bytes: bytes) -> dict | None:
    """Parst das erste VEVENT der text/calendar-Part. None, wenn keine
    Kalender-Part vorhanden oder kein VEVENT enthalten ist."""
    ics = _find_calendar_text(raw_bytes)
    if not ics:
        return None
    lines = _unfold(ics).splitlines()
    try:
        i0 = next(i for i, l in enumerate(lines) if l.upper().startswith("BEGIN:VEVENT"))
        i1 = next(i for i, l in enumerate(lines) if l.upper().startswith("END:VEVENT"))
    except StopIteration:
        return None

    # VALARM-Subkomponente rausfiltern (eigene DESCRIPTION = "Reminder")
    clean, skip = [], False
    for l in lines[i0 + 1:i1]:
        u = l.upper()
        if u.startswith("BEGIN:VALARM"):
            skip = True; continue
        if u.startswith("END:VALARM"):
            skip = False; continue
        if not skip:
            clean.append(l)

    props, attendees = _parse_props(clean)
    val = lambda n: props[n][1].strip() if n in props else ""

    location = _unescape(val("LOCATION"))
    description = _unescape(val("DESCRIPTION"))
    organizer = ""
    if "ORGANIZER" in props:
        p, v = props["ORGANIZER"]
        organizer = (p.get("CN") or v).replace("mailto:", "").strip()

    start, all_day = _parse_dt(*props["DTSTART"]) if "DTSTART" in props else (None, False)
    end, _ = _parse_dt(*props["DTEND"]) if "DTEND" in props else (None, False)

    join_url = _extract_join(val("URL"), location, description)
    mid = re.search(r"(?:Besprechungs-?ID|Meeting[- ]?ID)\D*([\d ]{8,})", description, re.I)
    code = re.search(r"(?:Passcode|Kenncode)\s*:?\s*(\S+)", description, re.I)

    return {
        "summary": _unescape(val("SUMMARY")),
        "uid": val("UID"),
        "when": _fmt_when(start, end, all_day),
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "all_day": all_day,
        "location": location,
        "organizer": organizer,
        "join_url": join_url,
        "meeting_id": mid.group(1).strip() if mid else "",
        "passcode": code.group(1).strip() if code else "",
        "attendee_count": len(attendees),
    }

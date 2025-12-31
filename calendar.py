#!/usr/bin/env python3
"""
Amsterdam Choghadiya + Kaal overlap + personal scoring -> 3 ICS calendars (GOOD / NEUTRAL / AVOID)

Key constraints satisfied:
- Uses Drik pages for Amsterdam (geoname-id=2759794).
- Parses Amsterdam-local times exactly as displayed by Drik.
- Does NOT use Indian time and does NOT convert IST->Amsterdam.

Requires:
  pip install requests beautifulsoup4
"""

from __future__ import annotations

# Avoid shadowing stdlib "calendar" when this file is named calendar.py.
import importlib
import os
import sys

import argparse
import hashlib
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

_this_dir = os.path.dirname(os.path.abspath(__file__))
for _p in ("", _this_dir):
    if _p in sys.path:
        sys.path.remove(_p)
_stdlib_calendar = importlib.import_module("calendar")
sys.modules["calendar"] = _stdlib_calendar
sys.path.insert(0, _this_dir)

import requests
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo


# ----------------------------
# Configuration
# ----------------------------

TZ = ZoneInfo("Europe/Amsterdam")
GEONAME_ID = 2759794

CHOGHADIYA_URL = "https://www.drikpanchang.com/muhurat/choghadiya.html"
DAY_PANCHANG_URL = "https://www.drikpanchang.com/panchang/day-panchang.html"

USER_AGENT = "Mozilla/5.0 (compatible; MuhuratCalendarBot/1.0)"
CACHE_DIR = ".drik_cache"
REQUEST_SLEEP_SECONDS = 5  # be polite

# Natal config (your kundli from earlier in this thread)
JANMA_NAKSHATRA = "Pushya"
JANMA_RASHI = "Karka"  # Cancer

# Toggle personalization. If False, uses only Choghadiya label + Kaal/vela rules.
USE_PERSONAL_SCORE = True

# Strict Kaal overlap for STARTS
START_BLOCKING_KAALS = {"Rahu Kalam", "Yamaganda", "Gulikai Kalam"}

# Vela tags treated as hard-avoid (both start and continue)
VELA_HARD_AVOID = {"Vaar Vela", "Kaal Vela", "Kaal Ratri"}


# ----------------------------
# Constants: Nakshatras & Rashis
# ----------------------------

NAKSHATRAS = [
    "Ashwini", "Bharani", "Krittika", "Rohini", "Mrigashira", "Ardra", "Punarvasu",
    "Pushya", "Ashlesha", "Magha", "Purva Phalguni", "Uttara Phalguni", "Hasta",
    "Chitra", "Swati", "Vishakha", "Anuradha", "Jyeshtha", "Mula", "Purva Ashadha",
    "Uttara Ashadha", "Shravana", "Dhanishtha", "Shatabhisha", "Purva Bhadrapada",
    "Uttara Bhadrapada", "Revati"
]
NAKSHATRA_IDX = {n: i + 1 for i, n in enumerate(NAKSHATRAS)}  # 1..27

RASHIS = ["Mesha", "Vrishabha", "Mithuna", "Karka", "Simha", "Kanya",
          "Tula", "Vrishchika", "Dhanu", "Makara", "Kumbha", "Meena"]
RASHI_IDX = {r: i + 1 for i, r in enumerate(RASHIS)}  # 1..12

# Choghadiya base points (personal score component)
CHOGH_POINTS = {
    "Amrita": 3,
    "Shubha": 2,
    "Labha": 2,
    "Chara": 1,
    "Udvega": -1,
    "Roga": -2,
    "Kala": -2,
}

# Tara Bala points by tara group number (1..9)
# 1 Janma, 2 Sampat, 3 Vipat, 4 Kshema, 5 Pratyari, 6 Sadhaka, 7 Naidhana, 8 Mitra, 9 Ati Mitra
TARA_POINTS = {1: -2, 2: 2, 3: -2, 4: 1, 5: -1, 6: 2, 7: -3, 8: 1, 9: 2}

# Chandra Bala supportive houses from natal Moon sign
CHANDRA_SUPPORT = {1, 3, 6, 7, 10, 11}


# ----------------------------
# Models
# ----------------------------

@dataclass(frozen=True)
class Window:
    name: str
    start: datetime
    end: datetime

@dataclass(frozen=True)
class TimelineEntry:
    name: str
    start: datetime
    end: datetime

@dataclass(frozen=True)
class ChoghadiyaBlock:
    name: str           # Amrita/Shubha/Labha/Chara/Roga/Kala/Udvega
    label: str          # Best/Good/Gain/Neutral/Evil/Loss/Bad (from Drik)
    start: datetime
    end: datetime
    vela_tag: Optional[str]
    overlap_kaals: Tuple[str, ...]
    transit_nakshatra: str
    transit_rashi: str
    score: int
    score_breakdown: str
    start_allowed: bool
    continue_allowed: bool
    bucket: str         # GOOD / NEUTRAL / AVOID


# ----------------------------
# Parsing helpers
# ----------------------------

TIME12 = r"\d{1,2}:\d{2}\s[AP]M"
DATE_SUFFIX = r"(?:,\s*(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{2}))?"
RANGE_RE = re.compile(
    rf"^(?P<start>{TIME12})\s+to\s+(?P<end>{TIME12}){DATE_SUFFIX}"
    rf"(?:\s+(?P<tag>Vaar Vela|Kaal Vela|Kaal Ratri))?$"
)
RANGE_SEARCH_RE = re.compile(
    rf"{TIME12}\s+to\s+{TIME12}{DATE_SUFFIX}(?:\s+(?:Vaar Vela|Kaal Vela|Kaal Ratri))?"
)

UPTO_RE = re.compile(
    rf"^(?P<name>.+?)\s+upto\s+(?P<time>{TIME12}){DATE_SUFFIX}$"
)

NAME_RE = re.compile(r"^(Amrita|Shubha|Labha|Chara|Roga|Kala|Udvega)\s*-\s*(.+)$")

def ddmmyyyy(d: date) -> str:
    return d.strftime("%d/%m/%Y")

def ensure_cache_dir() -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)

def cache_key(url: str, params: Dict[str, str]) -> str:
    items = "&".join([f"{k}={params[k]}" for k in sorted(params.keys())])
    h = hashlib.sha1(f"{url}?{items}".encode("utf-8")).hexdigest()
    return h

def fetch_html(url: str, params: Dict[str, str], refresh: bool = False) -> str:
    """
    Simple filesystem cache to avoid hammering Drik.
    """
    ensure_cache_dir()
    key = cache_key(url, params)
    path = os.path.join(CACHE_DIR, f"{key}.html")

    if not refresh and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    time.sleep(REQUEST_SLEEP_SECONDS)
    r = requests.get(
        url,
        params=params,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        timeout=30,
    )
    r.raise_for_status()
    html = r.text
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return html

def soup_tokens(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    toks = [t.strip() for t in soup.stripped_strings if t and t.strip()]
    return toks

def clean_time_token(token: str) -> str:
    """
    Drik sometimes appends 'Image: ...' directly to time text with no space.
    Example: '01:43 PM to 02:41 PMImage: Rahu Kalam'
    """
    if "Image:" in token:
        token = token.split("Image:", 1)[0]
    return token.strip()

def parse_month_day_suffix(base: date, mon_abbr: str, day_str: str) -> date:
    mon = datetime.strptime(mon_abbr, "%b").month
    day = int(day_str)
    year = base.year
    if mon < base.month:  # handle Dec -> Jan rollover
        year += 1
    return date(year, mon, day)

def parse_time_on(d: date, t12: str) -> datetime:
    t = datetime.strptime(t12, "%I:%M %p").time()
    return datetime(d.year, d.month, d.day, t.hour, t.minute, tzinfo=TZ)

def parse_range(base: date, token: str) -> Tuple[datetime, datetime, Optional[str]]:
    token = clean_time_token(token)
    m = RANGE_RE.match(token)
    if not m:
        raise ValueError(f"Could not parse time range token: {token!r}")

    start_dt = parse_time_on(base, m.group("start"))

    if m.group("mon") and m.group("day"):
        end_date = parse_month_day_suffix(base, m.group("mon"), m.group("day"))
    else:
        end_date = base
    end_dt = parse_time_on(end_date, m.group("end"))

    if end_dt <= start_dt:
        end_dt += timedelta(days=1)

    return start_dt, end_dt, m.group("tag")

def parse_upto(base: date, token: str) -> Optional[Tuple[str, datetime]]:
    """
    Parses: '<Name> upto HH:MM AM[, Mon DD]'
    Returns (name, end_dt) or None.
    """
    token = clean_time_token(token)
    m = UPTO_RE.match(token)
    if not m:
        return None

    name = m.group("name").strip()
    t12 = m.group("time")
    if m.group("mon") and m.group("day"):
        end_date = parse_month_day_suffix(base, m.group("mon"), m.group("day"))
    else:
        end_date = base
    end_dt = parse_time_on(end_date, t12)
    return name, end_dt

def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and a_end > b_start

def find_index(tokens: List[str], predicate) -> int:
    for i, t in enumerate(tokens):
        if predicate(t):
            return i
    raise ValueError("Marker not found")

def normalize_heading(t: str) -> str:
    return t.strip().lstrip("#").strip().lower()


# ----------------------------
# Drik parsing: Choghadiya blocks
# ----------------------------

def parse_choghadiya_blocks(base: date, refresh: bool = False) -> List[Tuple[str, str, str]]:
    """
    Returns 16 tuples: (name, label, time_range_token)
    Uses the Choghadiya cards for Day/Night and parses each row.
    """
    html = fetch_html(CHOGHADIYA_URL, {"geoname-id": str(GEONAME_ID), "date": ddmmyyyy(base)}, refresh=refresh)
    soup = BeautifulSoup(html, "html.parser")

    def parse_card(title: str) -> List[Tuple[str, str, str]]:
        node = soup.find(string=lambda s: s and s.strip() == title)
        if not node:
            raise ValueError(f"Could not find section heading: {title!r}")
        card = node.find_parent("div", class_="dpMuhurtaCard")
        if not card:
            raise ValueError(f"Could not find card container for {title!r}")

        rows = card.find_all("div", class_="dpMuhurtaRow")
        out: List[Tuple[str, str, str]] = []
        for row in rows:
            text = row.get_text(" ", strip=True)
            m = RANGE_SEARCH_RE.search(text)
            if not m:
                raise ValueError(f"Could not find time range in row: {text!r}")
            range_token = m.group(0).strip()
            name_part = text[:m.start()].strip()
            nm = NAME_RE.match(name_part)
            if not nm:
                raise ValueError(f"Could not parse choghadiya name/label from: {name_part!r}")
            out.append((nm.group(1), nm.group(2).strip(), range_token))

        if len(out) != 8:
            raise ValueError(f"Expected 8 choghadiya blocks, got {len(out)}. Parsed={out}")
        return out

    day_blocks = parse_card("Day Choghadiya")
    night_blocks = parse_card("Night Choghadiya")
    return day_blocks + night_blocks


# ----------------------------
# Drik parsing: Day Panchang (Kaals, Moonsign, Nakshatra)
# ----------------------------

def parse_kaal_windows(base: date, refresh: bool = False) -> List[Window]:
    """
    From Day Panchang page, reads Rahu Kalam, Yamaganda, Gulikai Kalam under 'Inauspicious Timings'
    """
    html = fetch_html(DAY_PANCHANG_URL, {"geoname-id": str(GEONAME_ID), "date": ddmmyyyy(base)}, refresh=refresh)
    soup = BeautifulSoup(html, "html.parser")

    card = None
    for c in soup.find_all("div", class_="dpTableCard"):
        if c.find("div", class_="dpTableKey", string=lambda s: s and s.strip() == "Rahu Kalam"):
            card = c
            break
    if not card:
        raise ValueError("Could not locate table card for inauspicious timings.")

    rows = card.find_all("div", class_="dpTableRow")
    values: Dict[str, str] = {}
    for row in rows:
        keys = row.find_all("div", class_="dpTableKey")
        for key in keys:
            label = key.get_text(" ", strip=True)
            val = key.find_next_sibling("div", class_="dpTableValue")
            if val:
                values[label] = val.get_text(" ", strip=True)

    windows: List[Window] = []
    for label in ["Rahu Kalam", "Yamaganda", "Gulikai Kalam"]:
        value_text = values.get(label)
        if not value_text:
            raise ValueError(f"Could not find time range for {label!r}")
        m = RANGE_SEARCH_RE.search(clean_time_token(value_text))
        if not m:
            raise ValueError(f"Could not parse time range for {label!r}: {value_text!r}")
        sdt, edt, _ = parse_range(base, m.group(0))
        windows.append(Window(label, sdt, edt))

    return windows

def parse_timeline_items_from_value(base: date, value_text: str, names: List[str]) -> List[Tuple[str, Optional[datetime]]]:
    text = clean_time_token(value_text)
    text = re.sub(r"\s+", " ", text).strip()
    names_alt = "|".join(re.escape(n) for n in names)
    upto_re = re.compile(
        rf"(?P<name>{names_alt})\s+upto\s+(?P<time>{TIME12})(?:,\s*(?P<mon>[A-Za-z]{{3}})\s+(?P<day>\d{{2}}))?"
    )
    full_re = re.compile(rf"(?P<name>{names_alt})\s+upto\s+Full\s+(?:Night|Day)")
    name_only_re = re.compile(rf"(?P<name>{names_alt})(?!\s+upto)")

    matches: List[Tuple[int, Tuple[str, Optional[datetime]]]] = []
    for m in upto_re.finditer(text):
        if m.group("mon") and m.group("day"):
            end_date = parse_month_day_suffix(base, m.group("mon"), m.group("day"))
        else:
            end_date = base
        end_dt = parse_time_on(end_date, m.group("time"))
        matches.append((m.start(), (m.group("name"), end_dt)))

    for m in full_re.finditer(text):
        matches.append((m.start(), (m.group("name"), None)))

    for m in name_only_re.finditer(text):
        matches.append((m.start(), (m.group("name"), None)))

    matches.sort(key=lambda x: x[0])
    items = [item for _, item in matches]

    cleaned: List[Tuple[str, Optional[datetime]]] = []
    for name, end_dt in items:
        if cleaned and cleaned[-1][0] == name and cleaned[-1][1] == end_dt:
            continue
        cleaned.append((name, end_dt))
    return cleaned

def build_timeline(base: date, first_start: datetime, items: List[Tuple[str, Optional[datetime]]]) -> List[TimelineEntry]:
    """
    items: [(name, end_dt_or_None), ...] in order
    Creates [start,end) segments. The last item with end None is extended far enough.
    """
    out: List[TimelineEntry] = []
    cur_start = first_start
    far_end = first_start + timedelta(days=3)  # safely beyond next sunrise

    for name, end_dt in items:
        if end_dt is None:
            out.append(TimelineEntry(name=name, start=cur_start, end=far_end))
            return out
        if end_dt <= cur_start:
            # if the page gives something weird, still force monotonicity
            end_dt = cur_start + timedelta(minutes=1)
        out.append(TimelineEntry(name=name, start=cur_start, end=end_dt))
        cur_start = end_dt

    # if everything had end_dt, extend last minimally
    if out:
        last = out[-1]
        out[-1] = TimelineEntry(name=last.name, start=last.start, end=far_end)
    return out

def parse_nakshatra_timeline(base: date, refresh: bool = False) -> List[TimelineEntry]:
    """
    Reads Nakshatra changes from the Panchang section (between 'Nakshatra' and 'Yoga').
    Produces a timeline starting at local midnight of the base date.
    """
    html = fetch_html(DAY_PANCHANG_URL, {"geoname-id": str(GEONAME_ID), "date": ddmmyyyy(base)}, refresh=refresh)
    soup = BeautifulSoup(html, "html.parser")

    keys = soup.find_all("div", class_="dpTableKey", string=lambda s: s and s.strip() == "Nakshatra")
    items: List[Tuple[str, Optional[datetime]]] = []
    for key in keys:
        val = key.find_next_sibling("div", class_="dpTableValue")
        if not val:
            continue
        items.extend(parse_timeline_items_from_value(base, val.get_text(" ", strip=True), NAKSHATRAS))

    if not items:
        raise ValueError("Could not parse Nakshatra timeline from Day Panchang page.")

    start0 = datetime(base.year, base.month, base.day, 0, 0, tzinfo=TZ)
    return build_timeline(base, start0, items)

def parse_moonsign_timeline(base: date, refresh: bool = False) -> List[TimelineEntry]:
    """
    Reads Moonsign changes from 'Rashi and Nakshatra' section:
      Moonsign
      Vrishabha upto ...
      Mithuna
    Produces a timeline starting at local midnight of base date.
    """
    html = fetch_html(DAY_PANCHANG_URL, {"geoname-id": str(GEONAME_ID), "date": ddmmyyyy(base)}, refresh=refresh)
    soup = BeautifulSoup(html, "html.parser")

    keys = soup.find_all("div", class_="dpTableKey", string=lambda s: s and s.strip() == "Moonsign")
    items: List[Tuple[str, Optional[datetime]]] = []
    for key in keys:
        val = key.find_next_sibling("div", class_="dpTableValue")
        if not val:
            continue
        items.extend(parse_timeline_items_from_value(base, val.get_text(" ", strip=True), RASHIS))

    if not items:
        raise ValueError("Could not parse Moonsign timeline from Day Panchang page.")

    start0 = datetime(base.year, base.month, base.day, 0, 0, tzinfo=TZ)
    return build_timeline(base, start0, items)

def timeline_value_at(t: datetime, tl: List[TimelineEntry]) -> str:
    for e in tl:
        if e.start <= t < e.end:
            return e.name
    # fallback
    return tl[-1].name


# ----------------------------
# Scoring
# ----------------------------

def tara_points(janma_nak: str, transit_nak: str) -> int:
    j = NAKSHATRA_IDX[janma_nak]      # 1..27
    tr = NAKSHATRA_IDX[transit_nak]   # 1..27
    d = (tr - j) % 27   # 0..26
    n = d + 1           # 1..27
    t = ((n - 1) % 9) + 1  # 1..9
    return TARA_POINTS[t]

def chandra_points(janma_rashi: str, transit_rashi: str) -> int:
    j = RASHI_IDX[janma_rashi]        # 1..12
    tr = RASHI_IDX[transit_rashi]     # 1..12
    house = ((tr - j) % 12) + 1       # 1..12
    if house in CHANDRA_SUPPORT:
        return 1
    if house == 8:
        return -2
    return -1

def compute_personal_score(choghadiya_name: str, transit_nak: str, transit_rashi: str) -> Tuple[int, str]:
    c = CHOGH_POINTS.get(choghadiya_name, 0)
    t = tara_points(JANMA_NAKSHATRA, transit_nak)
    m = chandra_points(JANMA_RASHI, transit_rashi)
    total = c + t + m
    breakdown = f"Chogh:{c}, Tara:{t}, Chandra:{m}, Total:{total}"
    return total, breakdown


# ----------------------------
# Bucketing rules (GOOD / NEUTRAL / AVOID)
# ----------------------------

def assign_bucket(
    chogh_name: str,
    vela_tag: Optional[str],
    overlap_kaals: Tuple[str, ...],
    score: int
) -> Tuple[str, bool, bool]:
    """
    Returns (bucket, start_allowed, continue_allowed)

    Rules:
    - Vela (Vaar/Kaal/Ratri) => avoid both start and continue
    - Any overlap with Rahu/Yamaganda/Gulikai => start not allowed
    - Continue can still be allowed if not vela and not inherently 'bad' choghadiya AND score isn't too negative
    - Buckets:
        GOOD:   start_allowed and score >= 3
        NEUTRAL:
            - not start_allowed and score >= 2   (continue ok; don't start)
            - OR start_allowed and 0..2
        AVOID: otherwise
    """
    is_vela = (vela_tag in VELA_HARD_AVOID)
    has_kaal = any(k in START_BLOCKING_KAALS for k in overlap_kaals)

    # hard blocks
    if is_vela:
        return "AVOID", False, False

    start_allowed = not has_kaal

    # base "bad" choghadiya are generally avoid
    is_bad_chogh = chogh_name in {"Roga", "Kala", "Udvega"}

    # continuation rule: allow continuations in kaal-overlap windows *if* not bad choghadiya and score not too low
    continue_allowed = (not is_bad_chogh) and (score >= 0)

    # If starts are allowed, continuation is also allowed unless bad
    if start_allowed and not is_bad_chogh:
        continue_allowed = True
    if start_allowed and is_bad_chogh:
        continue_allowed = False

    # Bucket
    if start_allowed and score >= 3:
        bucket = "GOOD"
    elif ((not start_allowed) and score >= 2) or (start_allowed and 0 <= score <= 2):
        bucket = "NEUTRAL"
    else:
        bucket = "AVOID"

    # If we bucketed NEUTRAL but continuation isn't allowed, force AVOID
    if bucket == "NEUTRAL" and not continue_allowed:
        bucket = "AVOID"

    # If we bucketed GOOD but it’s a bad choghadiya (shouldn't happen), force down
    if bucket == "GOOD" and is_bad_chogh:
        bucket = "NEUTRAL"

    return bucket, start_allowed, continue_allowed


# ----------------------------
# ICS writing
# ----------------------------

def ics_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")

def stable_uid(*parts: str) -> str:
    h = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    return f"{h}@muhurat-ams"

def write_ics(path: str, cal_name: str, events: List[Dict]) -> None:
    now = datetime.now(tz=TZ).strftime("%Y%m%dT%H%M%S")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//MuhuratAmsterdam//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(cal_name)}",
        "X-WR-TIMEZONE:Europe/Amsterdam",
    ]

    for e in events:
        dtstart = e["start"].strftime("%Y%m%dT%H%M%S")
        dtend = e["end"].strftime("%Y%m%dT%H%M%S")
        lines += [
            "BEGIN:VEVENT",
            f"UID:{e['uid']}",
            f"DTSTAMP:{now}",
            f"DTSTART;TZID=Europe/Amsterdam:{dtstart}",
            f"DTEND;TZID=Europe/Amsterdam:{dtend}",
            f"SUMMARY:{ics_escape(e['summary'])}",
            f"DESCRIPTION:{ics_escape(e['description'])}",
            "STATUS:CONFIRMED",
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\r\n".join(lines) + "\r\n")


# ----------------------------
# Pipeline: for each date -> blocks -> scored blocks -> events
# ----------------------------

def daterange(start: date, days: int) -> Iterable[date]:
    for i in range(days):
        yield start + timedelta(days=i)

def build_for_date(base: date, refresh: bool) -> List[ChoghadiyaBlock]:
    # Parse inputs
    raw_blocks = parse_choghadiya_blocks(base, refresh=refresh)
    kaals = parse_kaal_windows(base, refresh=refresh)
    nak_tl = parse_nakshatra_timeline(base, refresh=refresh)
    rashi_tl = parse_moonsign_timeline(base, refresh=refresh)

    blocks: List[ChoghadiyaBlock] = []

    for name, label, range_token in raw_blocks:
        start_dt, end_dt, vela_tag = parse_range(base, range_token)

        overlap = tuple(sorted({
            w.name for w in kaals if overlaps(start_dt, end_dt, w.start, w.end)
        }))

        transit_nak = timeline_value_at(start_dt, nak_tl)
        transit_rashi = timeline_value_at(start_dt, rashi_tl)

        if USE_PERSONAL_SCORE:
            score, breakdown = compute_personal_score(name, transit_nak, transit_rashi)
        else:
            # Non-personal: treat choghadiya "good/neutral/bad" only
            score = CHOGH_POINTS.get(name, 0)
            breakdown = f"Chogh:{score} (no Tara/Chandra)"

        bucket, start_allowed, continue_allowed = assign_bucket(name, vela_tag, overlap, score)

        blocks.append(ChoghadiyaBlock(
            name=name,
            label=label,
            start=start_dt,
            end=end_dt,
            vela_tag=vela_tag,
            overlap_kaals=overlap,
            transit_nakshatra=transit_nak,
            transit_rashi=transit_rashi,
            score=score,
            score_breakdown=breakdown,
            start_allowed=start_allowed,
            continue_allowed=continue_allowed,
            bucket=bucket
        ))

    return blocks

def generate_calendars(start: date, days: int, outdir: str, refresh: bool) -> None:
    os.makedirs(outdir, exist_ok=True)

    good_events: List[Dict] = []
    neutral_events: List[Dict] = []
    avoid_events: List[Dict] = []

    for d in daterange(start, days):
        blocks = build_for_date(d, refresh=refresh)

        for b in blocks:
            tags = []
            if b.vela_tag:
                tags.append(b.vela_tag)
            tags.extend(list(b.overlap_kaals))

            desc = "\n".join([
                f"Score: {b.score} ({b.score_breakdown})" if USE_PERSONAL_SCORE else f"Score: {b.score}",
                f"Transit: {b.transit_nakshatra} / {b.transit_rashi}",
                f"Start: {'Allowed' if b.start_allowed else 'Avoid'}",
                f"Continue: {'Allowed' if b.continue_allowed else 'Avoid'}",
                f"Tags: {', '.join(tags) if tags else 'None'}",
                f"Location: Amsterdam (geoname-id={GEONAME_ID})",
                f"Source: drikpanchang.com",
            ])

            uid = stable_uid(b.bucket, d.isoformat(), b.name, b.start.isoformat(), b.end.isoformat())

            ev = {
                "uid": uid,
                "start": b.start,
                "end": b.end,
                "summary": b.name,  # minimal title
                "description": desc,
            }

            if b.bucket == "GOOD":
                good_events.append(ev)
            elif b.bucket == "NEUTRAL":
                neutral_events.append(ev)
            else:
                avoid_events.append(ev)

    write_ics(os.path.join(outdir, "choghadiya_good.ics"), "Choghadiya GOOD (Amsterdam)", good_events)
    write_ics(os.path.join(outdir, "choghadiya_neutral.ics"), "Choghadiya NEUTRAL (Amsterdam)", neutral_events)
    write_ics(os.path.join(outdir, "choghadiya_avoid.ics"), "Choghadiya AVOID (Amsterdam)", avoid_events)


# ----------------------------
# CLI
# ----------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate Amsterdam Choghadiya calendars (GOOD/NEUTRAL/AVOID) from Drik Panchang")
    p.add_argument("--start", type=str, default="2026-01-01", help="Start date (YYYY-MM-DD)")
    p.add_argument("--days", type=int, default=14, help="Number of days to generate (keep modest; subscriptions can be rolling)")
    p.add_argument("--outdir", type=str, default="out_ics", help="Output directory for .ics files")
    p.add_argument("--refresh", action="store_true", help="Ignore cache and refetch pages")
    return p.parse_args()

def main() -> None:
    args = parse_args()
    y, m, d = [int(x) for x in args.start.split("-")]
    start_d = date(y, m, d)

    generate_calendars(start_d, args.days, args.outdir, refresh=args.refresh)
    print(f"Done. Files written to: {args.outdir}/")
    print(" - choghadiya_good.ics")
    print(" - choghadiya_neutral.ics")
    print(" - choghadiya_avoid.ics")

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Amsterdam Choghadiya + Kaal/Abhijit overlap + personal scoring -> 3 ICS calendars (GOOD / NEUTRAL / AVOID)

Key constraints satisfied:
- Uses Drik pages for Amsterdam (geoname-id=2759794).
- Parses Amsterdam-local times exactly as displayed by Drik.
- Does NOT use Indian time and does NOT convert IST->Amsterdam.

Requires:
  pip install requests beautifulsoup4
"""

from __future__ import annotations

import glob
import importlib
import os
import subprocess
import sys

import argparse
import hashlib
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

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
REQUEST_SLEEP_SECONDS = 0.4  # be polite
DOTENV_PATH = ".env"
KUNDALI_PROFILE_PATH = os.path.join(_this_dir, "kundali_profile.yaml")

# Toggle personalization. If False, uses only Choghadiya label + Kaal/vela rules.
USE_PERSONAL_SCORE = True

# Strict Kaal overlap for STARTS
START_BLOCKING_KAALS = {"Rahu Kalam", "Yamaganda", "Gulikai Kalam"}

# Vela tags that cap GOOD and add start risk penalty
VELA_TAGS = {"Vaar Vela", "Kaal Vela", "Kaal Ratri"}

GITHUB_REMOTE_SUBSTR = "github.com/devraghu/mahurat"
SKIP_GITHUB_PUBLISH_ENV = "MAHURAT_SKIP_GITHUB_PUBLISH"


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
TARA_POINTS = {1: -1, 2: 2, 3: -1, 4: 1, 5: -1, 6: 2, 7: -2, 8: 1, 9: 2}

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
class DashaPeriod:
    md: str
    start: date
    end: date
    ad_ranges: Tuple[Tuple[str, date, date], ...]

@dataclass(frozen=True)
class KundaliProfile:
    janma_nakshatra: str
    janma_rashi: str
    dasha_periods: Tuple[DashaPeriod, ...]

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
    has_abhijit: bool
    transit_nakshatra: str
    transit_rashi: str
    base_score: float
    start_score: float
    continue_score: float
    score_breakdown: str
    start_allowed: bool
    continue_allowed: bool
    bucket: str         # GOOD / NEUTRAL / AVOID


# ----------------------------
# Parsing helpers
# ----------------------------

TIME12 = r"\d{1,2}:\d{2}\s[AP]M"
DATE_SUFFIX = r"(?:\s*,\s*(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{2}))?"
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

    start_t = datetime.strptime(m.group("start"), "%I:%M %p").time()
    end_t = datetime.strptime(m.group("end"), "%I:%M %p").time()

    if m.group("mon") and m.group("day"):
        end_date = parse_month_day_suffix(base, m.group("mon"), m.group("day"))
    else:
        end_date = base

    start_date = base
    if end_date != base:
        if start_t <= end_t and start_t.hour < 12:
            start_date = end_date

    start_dt = datetime(start_date.year, start_date.month, start_date.day, start_t.hour, start_t.minute, tzinfo=TZ)
    end_dt = datetime(end_date.year, end_date.month, end_date.day, end_t.hour, end_t.minute, tzinfo=TZ)

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


def load_dotenv(path: str) -> Dict[str, str]:
    """
    Minimal `.env` loader. Returns a dict of KEY=VAL entries.
    Lines starting with '#' or empty lines are ignored.
    """
    out: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                out[key] = val
    except FileNotFoundError:
        pass
    return out


# ----------------------------
# Kundali profile (YAML)
# ----------------------------

def _strip_yaml_comment(line: str) -> str:
    out = []
    in_single = False
    in_double = False
    for ch in line:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            break
        out.append(ch)
    return "".join(out).rstrip()

def _find_unquoted_colon(text: str) -> int:
    in_single = False
    in_double = False
    for i, ch in enumerate(text):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == ":" and not in_single and not in_double:
            return i
    return -1

def _split_yaml_key_val(text: str) -> Tuple[str, Optional[str]]:
    idx = _find_unquoted_colon(text)
    if idx == -1:
        raise ValueError(f"Invalid YAML mapping line: {text!r}")
    key = text[:idx].strip()
    val = text[idx + 1 :].strip()
    if not val:
        return key, None
    return key, val

def _parse_yaml_scalar(val: str) -> Any:
    if val == "[]":
        return []
    if val == "{}":
        return {}
    if val.startswith("{") and val.endswith("}"):
        return _parse_inline_map(val)
    if val.startswith("'") and val.endswith("'"):
        return val[1:-1]
    if val.startswith('"') and val.endswith('"'):
        return val[1:-1]
    low = val.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    return val

def _split_inline_items(text: str) -> List[str]:
    items: List[str] = []
    buf: List[str] = []
    in_single = False
    in_double = False
    for ch in text:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        if ch == "," and not in_single and not in_double:
            items.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    if buf:
        items.append("".join(buf).strip())
    return [i for i in items if i]

def _parse_inline_map(text: str) -> Dict[str, Any]:
    inner = text.strip()[1:-1].strip()
    if not inner:
        return {}
    out: Dict[str, Any] = {}
    for part in _split_inline_items(inner):
        key, val = _split_yaml_key_val(part)
        out[key] = _parse_yaml_scalar(val) if val is not None else None
    return out

def _next_non_empty(lines: List[str], start: int) -> Optional[Tuple[int, str]]:
    for raw in lines[start:]:
        clean = _strip_yaml_comment(raw)
        if not clean.strip():
            continue
        indent = len(clean) - len(clean.lstrip(" "))
        if indent % 2 != 0:
            raise ValueError(f"Invalid indentation in YAML line: {raw!r}")
        level = indent // 2
        return level, clean.strip()
    return None

def parse_simple_yaml(text: str) -> Dict[str, Any]:
    lines = text.splitlines()
    root: Dict[str, Any] = {}
    stack: List[Tuple[int, Any]] = [(-1, root)]

    i = 0
    while i < len(lines):
        raw = lines[i]
        clean = _strip_yaml_comment(raw)
        if not clean.strip():
            i += 1
            continue
        indent = len(clean) - len(clean.lstrip(" "))
        if indent % 2 != 0:
            raise ValueError(f"Invalid indentation in YAML line: {raw!r}")
        level = indent // 2
        text = clean.strip()

        while stack and stack[-1][0] >= level:
            stack.pop()
        parent = stack[-1][1]

        if text.startswith("- "):
            if not isinstance(parent, list):
                raise ValueError(f"List item found but parent is not a list: {text!r}")
            item_text = text[2:].strip()
            if item_text.startswith("{") and item_text.endswith("}"):
                parent.append(_parse_inline_map(item_text))
            elif _find_unquoted_colon(item_text) != -1:
                key, val = _split_yaml_key_val(item_text)
                item: Dict[str, Any] = {}
                item[key] = _parse_yaml_scalar(val) if val is not None else None
                parent.append(item)
                stack.append((level, item))
            else:
                parent.append(_parse_yaml_scalar(item_text))
            i += 1
            continue

        key, val = _split_yaml_key_val(text)
        if isinstance(parent, list):
            if parent and isinstance(parent[-1], dict):
                parent = parent[-1]
            else:
                raise ValueError(f"Mapping line inside list without a dict item: {text!r}")

        if val is None:
            nxt = _next_non_empty(lines, i + 1)
            if nxt and nxt[0] > level and nxt[1].startswith("- "):
                container: Any = []
            else:
                container = {}
            parent[key] = container
            stack.append((level, container))
        else:
            parent[key] = _parse_yaml_scalar(val)
        i += 1

    return root

def normalize_lord(name: str) -> str:
    short = {
        "VEN": "Venus",
        "SUN": "Sun",
        "MON": "Moon",
        "MAR": "Mars",
        "RAH": "Rahu",
        "JUP": "Jupiter",
        "SAT": "Saturn",
        "KET": "Ketu",
        "MER": "Mercury",
    }
    key = name.strip()
    key_upper = key.upper()
    if key_upper in short:
        return short[key_upper]
    return key.title()

def normalize_nakshatra(name: str) -> str:
    alias = {
        "Pashyami": "Pushya",
    }
    return alias.get(name, name)

def load_kundali_profile(path: str) -> KundaliProfile:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = parse_simple_yaml(f.read())
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Missing kundali profile file: {path}") from exc

    anchors = data.get("birth_anchors", {})
    janma_nak = anchors.get("janma_nakshatra", {})
    janma_nak_val = (
        janma_nak.get("normalized_english")
        or janma_nak.get("name_in_pdf")
        or ""
    )
    janma_nak_val = normalize_nakshatra(janma_nak_val)
    if not janma_nak_val:
        raise ValueError("Janma nakshatra missing in kundali_profile.yaml")

    janma_rashi = anchors.get("janma_rashi", {})
    rashi_val = janma_rashi.get("sanskrit") or janma_rashi.get("english") or ""
    if not rashi_val:
        raise ValueError("Janma rashi missing in kundali_profile.yaml")

    sign_map = data.get("sign_name_mapping", {})
    if rashi_val in sign_map:
        rashi_val = sign_map[rashi_val]

    dasha_root = data.get("vimshottari_dasha_lahiri", {})
    md_periods = dasha_root.get("mahadasha_periods", [])
    dasha_periods: List[DashaPeriod] = []
    for md in md_periods:
        md_lord = normalize_lord(md.get("lord", ""))
        md_start = date.fromisoformat(md.get("start"))
        md_end = date.fromisoformat(md.get("end"))
        ad_ranges: List[Tuple[str, date, date]] = []
        ad_list = md.get("antardasha_periods", [])
        if not isinstance(ad_list, list):
            ad_list = []
        for ad in ad_list:
            if not isinstance(ad, dict):
                continue
            ad_lord = normalize_lord(ad.get("lord", ""))
            ad_start = date.fromisoformat(ad.get("start"))
            ad_end = date.fromisoformat(ad.get("end"))
            ad_ranges.append((ad_lord, ad_start, ad_end))
        dasha_periods.append(
            DashaPeriod(md=md_lord, start=md_start, end=md_end, ad_ranges=tuple(ad_ranges))
        )

    if not dasha_periods:
        raise ValueError("No dasha periods found in kundali_profile.yaml")

    return KundaliProfile(
        janma_nakshatra=janma_nak_val,
        janma_rashi=rashi_val,
        dasha_periods=tuple(dasha_periods),
    )


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

def parse_abhijit_window(base: date, refresh: bool = False) -> Optional[Window]:
    """
    From Day Panchang page, reads Abhijit Muhurat under 'Auspicious Timings'
    """
    html = fetch_html(DAY_PANCHANG_URL, {"geoname-id": str(GEONAME_ID), "date": ddmmyyyy(base)}, refresh=refresh)
    soup = BeautifulSoup(html, "html.parser")

    card = None
    for c in soup.find_all("div", class_="dpTableCard"):
        if c.find("div", class_="dpTableKey", string=lambda s: s and s.strip().startswith("Abhijit")):
            card = c
            break
    if not card:
        return None

    values: Dict[str, str] = {}
    for row in card.find_all("div", class_="dpTableRow"):
        keys = row.find_all("div", class_="dpTableKey")
        for key in keys:
            label = key.get_text(" ", strip=True)
            val = key.find_next_sibling("div", class_="dpTableValue")
            if val:
                values[label] = val.get_text(" ", strip=True)

    for label, value_text in values.items():
        if label.startswith("Abhijit"):
            m = RANGE_SEARCH_RE.search(clean_time_token(value_text))
            if not m:
                raise ValueError(f"Could not parse time range for Abhijit: {value_text!r}")
            sdt, edt, _ = parse_range(base, m.group(0))
            return Window("Abhijit Muhurat", sdt, edt)

    return None

def parse_timeline_items_from_value(base: date, value_text: str, names: List[str]) -> List[Tuple[str, Optional[datetime]]]:
    text = clean_time_token(value_text)
    text = re.sub(r"\s+", " ", text).strip()
    names_alt = "|".join(re.escape(n) for n in names)
    upto_re = re.compile(
        rf"(?P<name>{names_alt})\s+upto\s+(?P<time>{TIME12}){DATE_SUFFIX}"
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
# Dasha (Lahiri Vimshottari)
# ----------------------------

KUNDALI_PROFILE = load_kundali_profile(KUNDALI_PROFILE_PATH)

DASHA_AD_BONUS = {
    "Jupiter": 1.0,
    "Mercury": 1.0,
    "Venus": 1.0,
    "Saturn": 0.5,
    "Mars": 0.5,
    "Rahu": 0.5,
    "Ketu": 0.0,
    "Sun": 0.0,
    "Moon": 0.0,
}

def dasha_lords_for(d: date) -> Tuple[str, str]:
    for period in KUNDALI_PROFILE.dasha_periods:
        if period.start <= d < period.end:
            md_lord = period.md
            ad_lord = md_lord
            for candidate, ad_start, ad_end in period.ad_ranges:
                if ad_start <= d < ad_end:
                    ad_lord = candidate
                    break
            return md_lord, ad_lord
    raise ValueError(f"No dasha period found for date {d.isoformat()}")

def dasha_objective_bonus(d: date) -> Tuple[float, str, str]:
    md_lord, ad_lord = dasha_lords_for(d)
    bonus = 0.0
    if md_lord == "Venus":
        bonus += 0.5
    bonus += DASHA_AD_BONUS.get(ad_lord, 0.0)
    return bonus, md_lord, ad_lord


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
        return -1
    return 0

def fmt_score(val: float) -> str:
    return f"{val:g}"

def compute_personal_score(
    choghadiya_name: str,
    transit_nak: str,
    transit_rashi: str,
    on_date: date,
) -> Tuple[float, str]:
    c = CHOGH_POINTS.get(choghadiya_name, 0)
    t = tara_points(KUNDALI_PROFILE.janma_nakshatra, transit_nak)
    m = chandra_points(KUNDALI_PROFILE.janma_rashi, transit_rashi)
    dasha_bonus, md_lord, ad_lord = dasha_objective_bonus(on_date)
    total = c + t + m + dasha_bonus
    breakdown = (
        f"Chogh:{c}, Tara:{t}, Chandra:{m}, "
        f"Dasha:{fmt_score(dasha_bonus)} (MD:{md_lord} AD:{ad_lord})"
    )
    return total, breakdown


# ----------------------------
# Bucketing rules (GOOD / NEUTRAL / AVOID)
# ----------------------------

def assign_bucket(
    chogh_name: str,
    vela_tag: Optional[str],
    overlap_kaals: Tuple[str, ...],
    base_score: float,
    start_score: float,
    continue_score: float,
) -> Tuple[str, bool, bool]:
    """
    Returns (bucket, start_allowed, continue_allowed)

    Rules:
    - Kaals/Vela cap GOOD (no GOOD if any overlap)
    - AVOID is rare: ContinueScore <= -3 OR stacked negativity in bad choghadiya + low BaseScore + Kaal/Vela
    - GOOD: no Kaal/Vela and StartScore >= 2
    - NEUTRAL: everything else
    """
    has_vela = (vela_tag in VELA_TAGS)
    has_kaal = any(k in START_BLOCKING_KAALS for k in overlap_kaals)
    is_bad_chogh = chogh_name in {"Roga", "Kala", "Udvega"}

    if continue_score <= -3:
        return "AVOID", False, False

    if is_bad_chogh and base_score <= -2 and (has_kaal or has_vela):
        return "AVOID", False, False

    if not has_kaal and not has_vela and start_score >= 2:
        return "GOOD", True, True

    # NEUTRAL
    continue_allowed = True
    if has_kaal or has_vela:
        start_allowed = start_score >= 1
    else:
        start_allowed = True
    return "NEUTRAL", start_allowed, continue_allowed


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
# GitHub publishing
# ----------------------------

def _git_output(cmd: List[str], cwd: str) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, check=True)
    return proc.stdout.strip()

def publish_to_github(outdir: str) -> None:
    if os.environ.get(SKIP_GITHUB_PUBLISH_ENV):
        print(f"Skipping GitHub publish because {SKIP_GITHUB_PUBLISH_ENV} is set.")
        return

    repo_root = _git_output(["git", "rev-parse", "--show-toplevel"], cwd=os.getcwd())
    remote_url = _git_output(["git", "remote", "get-url", "origin"], cwd=repo_root)
    if GITHUB_REMOTE_SUBSTR not in remote_url:
        raise RuntimeError(f"Unexpected Git remote ({remote_url}); publish target must be {GITHUB_REMOTE_SUBSTR}.")

    branch = _git_output(["git", "symbolic-ref", "--short", "HEAD"], cwd=repo_root)

    abs_outdir = os.path.abspath(outdir)
    if not abs_outdir.startswith(repo_root):
        raise RuntimeError(f"Output directory {abs_outdir} is not inside the repository root {repo_root}.")

    ics_paths = sorted(glob.glob(os.path.join(abs_outdir, "*.ics")))
    if not ics_paths:
        raise RuntimeError(f"No .ics files found in {abs_outdir} to publish.")

    rel_paths = [os.path.relpath(p, repo_root) for p in ics_paths]
    subprocess.run(["git", "add"] + rel_paths, cwd=repo_root, check=True)

    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo_root)
    if diff.returncode == 0:
        print("No changes to choghadiya ICS files; skipping publish.")
        return

    commit_msg = f"Update choghadiya calendars {date.today().isoformat()}"
    subprocess.run(["git", "commit", "-m", commit_msg], cwd=repo_root, check=True)
    dotenv = load_dotenv(os.path.join(repo_root, DOTENV_PATH))
    pat = dotenv.get("GITHUB_PAT") or os.environ.get("GITHUB_PAT")
    if not pat:
        raise RuntimeError(
            "GITHUB_PAT must be provided via .env or environment to publish to GitHub."
        )

    askpass = os.path.join(repo_root, "scripts", "git-askpass.sh")
    if not os.path.exists(askpass):
        raise RuntimeError("Missing scripts/git-askpass.sh helper for password-less git pushes.")

    push_env = os.environ.copy()
    push_env["GIT_TERMINAL_PROMPT"] = "0"
    push_env["GIT_ASKPASS"] = askpass
    push_env["GITHUB_PAT"] = pat
    subprocess.run(
        ["git", "push", "origin", branch],
        cwd=repo_root,
        env=push_env,
        check=True,
    )


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
    abhijit = parse_abhijit_window(base, refresh=refresh)
    nak_tl = parse_nakshatra_timeline(base, refresh=refresh)
    rashi_tl = parse_moonsign_timeline(base, refresh=refresh)

    blocks: List[ChoghadiyaBlock] = []

    for name, label, range_token in raw_blocks:
        start_dt, end_dt, vela_tag = parse_range(base, range_token)

        overlap = tuple(sorted({
            w.name for w in kaals if overlaps(start_dt, end_dt, w.start, w.end)
        }))
        has_kaal = any(k in START_BLOCKING_KAALS for k in overlap)
        has_vela = vela_tag in VELA_TAGS
        has_abhijit = bool(abhijit) and overlaps(start_dt, end_dt, abhijit.start, abhijit.end)
        abhijit_bonus = 1.0 if has_abhijit else 0.0
        start_risk_penalty = (1.0 if has_kaal else 0.0) + (1.0 if has_vela else 0.0)

        transit_nak = timeline_value_at(start_dt, nak_tl)
        transit_rashi = timeline_value_at(start_dt, rashi_tl)

        if USE_PERSONAL_SCORE:
            base_score, breakdown = compute_personal_score(name, transit_nak, transit_rashi, start_dt.date())
        else:
            # Non-personal: treat choghadiya "good/neutral/bad" only
            base_score = float(CHOGH_POINTS.get(name, 0))
            breakdown = f"Chogh:{fmt_score(base_score)} (no Tara/Chandra/Dasha)"

        start_score = base_score + abhijit_bonus - start_risk_penalty
        continue_score = base_score + abhijit_bonus

        bucket, start_allowed, continue_allowed = assign_bucket(
            name,
            vela_tag,
            overlap,
            base_score,
            start_score,
            continue_score,
        )

        blocks.append(ChoghadiyaBlock(
            name=name,
            label=label,
            start=start_dt,
            end=end_dt,
            vela_tag=vela_tag,
            overlap_kaals=overlap,
            has_abhijit=has_abhijit,
            transit_nakshatra=transit_nak,
            transit_rashi=transit_rashi,
            base_score=base_score,
            start_score=start_score,
            continue_score=continue_score,
            score_breakdown=breakdown,
            start_allowed=start_allowed,
            continue_allowed=continue_allowed,
            bucket=bucket
        ))

    return blocks

def debug_print_date(base: date, refresh: bool) -> None:
    blocks = build_for_date(base, refresh=refresh)
    dasha_bonus, md_lord, ad_lord = dasha_objective_bonus(base)
    print(f"Debug blocks for {base.isoformat()} (Europe/Amsterdam)")
    print(f"Dasha: MD={md_lord} AD={ad_lord} Bonus={fmt_score(dasha_bonus)}")
    for b in blocks:
        has_kaal = any(k in START_BLOCKING_KAALS for k in b.overlap_kaals)
        has_vela = b.vela_tag in VELA_TAGS
        print(
            f"{b.name} | {b.start.isoformat()} -> {b.end.isoformat()} | "
            f"hasKaal={has_kaal} hasVela={has_vela} hasAbhijit={b.has_abhijit} | "
            f"BaseScore={fmt_score(b.base_score)} StartScore={fmt_score(b.start_score)} "
            f"ContinueScore={fmt_score(b.continue_score)} | "
            f"bucket={b.bucket} StartAllowed={b.start_allowed} ContinueAllowed={b.continue_allowed}"
        )

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
            if b.has_abhijit:
                tags.append("Abhijit Muhurat")
            tags.extend(list(b.overlap_kaals))

            desc = "\n".join([
                f"BaseScore: {fmt_score(b.base_score)} ({b.score_breakdown})" if USE_PERSONAL_SCORE else f"BaseScore: {fmt_score(b.base_score)}",
                f"StartScore: {fmt_score(b.start_score)}",
                f"ContinueScore: {fmt_score(b.continue_score)}",
                f"Abhijit: {'Yes' if b.has_abhijit else 'No'}",
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
    p.add_argument("--start", type=str, default=None, help="Start date (YYYY-MM-DD), defaults to today")
    p.add_argument("--days", type=int, default=30, help="Number of days to generate (default 30 to keep weekly cache warm)")
    p.add_argument("--outdir", type=str, default="out_ics", help="Output directory for .ics files")
    p.add_argument("--refresh", action="store_true", help="Ignore cache and refetch pages")
    p.add_argument("--debug-date", type=str, default=None, help="Print all 16 blocks with scores/buckets for a date (YYYY-MM-DD) and exit")
    p.add_argument("--skip-publish", action="store_true", help="Do not commit/push generated files (for local debugging)")
    return p.parse_args()

def main() -> None:
    args = parse_args()
    if args.debug_date:
        debug_d = date.fromisoformat(args.debug_date)
        debug_print_date(debug_d, refresh=args.refresh)
        return
    if args.start:
        start_d = date.fromisoformat(args.start)
    else:
        start_d = date.today()

    generate_calendars(start_d, args.days, args.outdir, refresh=args.refresh)
    if not args.skip_publish:
        publish_to_github(args.outdir)
    print(f"Done. Files written to: {args.outdir}/")
    print(" - choghadiya_good.ics")
    print(" - choghadiya_neutral.ics")
    print(" - choghadiya_avoid.ics")

if __name__ == "__main__":
    main()

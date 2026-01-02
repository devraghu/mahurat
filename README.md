# Amsterdam Muhurat Calendar Generator (Choghadiya + Kaals) — README

## Overview

This project generates **three iCalendar (.ics) calendars** for **Amsterdam, Netherlands** (timezone: `Europe/Amsterdam`) using **Drik Panchang** as the authoritative source of timings:

1. **GOOD** — recommended for **starting new actions** and **continuations**
2. **NEUTRAL** — **avoid starting**, but generally OK for **continuations**
3. **AVOID** — avoid even for continuations

Each day has **16 Choghadiya blocks** (8 day + 8 night) which are distributed across the three calendars according to strict rules and (optionally) personalized scoring.

The calendars are intended for use in:
- **Google Calendar** (subscribe by URL or import `.ics`)
- **Zenkit** (subscribe by iCal URL)

---

## Critical Constraints (Non-Negotiable)

### 1) No Indian Time (IST), Ever
- **Never compute in IST**
- **Never convert IST → Amsterdam**
- **Never use a website’s India timings and convert them**
- The only valid timings are those displayed by **Drik Panchang for Amsterdam**.

### 2) Only Drik Panchang as Timing Source (Amsterdam-local)
The program parses Drik’s pages for **Amsterdam** using `geoname-id=2759794`. Drik provides timings in **local time** with **DST adjustments**.

### 3) All produced timestamps must be timezone-aware
Every datetime used in the program must be:
- timezone-aware
- interpreted in `Europe/Amsterdam`
- emitted in ICS as `DTSTART;TZID=Europe/Amsterdam:` and `DTEND;TZID=Europe/Amsterdam:`

---

## Output Calendars

### Files Generated
The script outputs three `.ics` files (names may vary by config):
- `choghadiya_good.ics`
- `choghadiya_neutral.ics`
- `choghadiya_avoid.ics`

### Event Count Expectations
- **16 Choghadiya events per day** (8 day + 8 night)
- Each Choghadiya segment appears in **exactly one** of the three calendars.

### Event Title Policy (Minimal Titles)
`SUMMARY` must remain **minimal**, typically just the Choghadiya name:
- `Amrita`, `Shubha`, `Labha`, `Chara`, `Roga`, `Kala`, `Udvega`

### Description Policy (Verbose Allowed)
`DESCRIPTION` can include:
- scoring breakdown
- moon transit (nakshatra / rashi)
- start/continue suitability
- overlap tags (e.g., Rahu Kalam)
- source metadata

---

## Data Sources (Drik Panchang)

### Choghadiya (Amsterdam-local)
Choghadiya page for Amsterdam:

https://www.drikpanchang.com/muhurat/choghadiya.html?geoname-id=2759794&date=DD/MM/YYYY

Example:

https://www.drikpanchang.com/muhurat/choghadiya.html?geoname-id=2759794&date=01/01/2026


This page includes:
- Day Choghadiya blocks (8)
- Night Choghadiya blocks (8)
- Possible tags: `Vaar Vela`, `Kaal Vela`, `Kaal Ratri`
- Some time ranges cross midnight and may include a next-day suffix (e.g., `, Jan 02`). So make sure they are included in next calendar day instead of calendar day of provided DD/MM/YYYY

### Day Panchang (Amsterdam-local) for Kaals + Moon Transits
Day Panchang page for Amsterdam:


https://www.drikpanchang.com/panchang/day-panchang.html?geoname-id=2759794&date=DD/MM/YYYY

Example:

https://www.drikpanchang.com/panchang/day-panchang.html?geoname-id=2759794&date=01/01/2026


The program extracts:
- **Inauspicious Timings**:  
  - `Rahu Kalam`
  - `Yamaganda`
  - `Gulikai Kalam`
- Moon transits from page sections:
  - **Nakshatra** timeline (e.g., `Pushya upto 10:34 PM`)
  - **Moonsign** timeline (e.g., `Karka upto ...`)

> NOTE: Parsing must handle Drik’s formatting quirks, such as icon tokens like `Image:` appearing adjacent to time ranges.

---

## Core Logic

### 1) Parse Choghadiya Blocks (16 per day)
Each Choghadiya block has:
- `name`: Amrita / Shubha / Labha / Chara / Roga / Kala / Udvega
- `label`: human label such as Best/Good/Gain/Neutral/Evil/Loss/Bad (informational)
- `start`, `end`: timezone-aware Amsterdam-local datetimes
- `vela_tag` (optional): `Vaar Vela`, `Kaal Vela`, or `Kaal Ratri`

**Parsing requirements**
- Do not assume a fixed HTML structure.
- Do not assume the time range is “the next line.”
- Use robust token parsing:
  - recognize `"<Name> - <Label>"` tokens
  - then pair with subsequent `"HH:MM AM to HH:MM PM[, Mon DD][ tag]"` token
- Strip any junk appended to time tokens (e.g., `Image: ...`).

### 2) Parse Kaal Windows (Start-blocking Kaals)
Extract these time windows from Day Panchang:
- `Rahu Kalam`
- `Yamaganda`
- `Gulikai Kalam`

Each is a `[start, end)` interval in `Europe/Amsterdam`.

### 3) Parse Moon Transit Timelines (for personalization)
From Day Panchang:
- Nakshatra timeline
- Moonsign timeline

The generator assigns a transit state for each Choghadiya block (typically evaluated at block start):
- `transit_nakshatra = timeline_value_at(start_time)`
- `transit_rashi = timeline_value_at(start_time)`

---

## Suitability Rules (Starts vs Continuations)

### Strict Start Blocking by Kaals
If a Choghadiya block overlaps any of:
- `Rahu Kalam`
- `Yamaganda`
- `Gulikai Kalam`

Then:
- `StartAllowed = False`

### Vela Hard Avoid (Starts and Continuations)
If a block is tagged as:
- `Vaar Vela`
- `Kaal Vela`
- `Kaal Ratri`

Then:
- `StartAllowed = False`
- `ContinueAllowed = False`
- Bucket must be **AVOID**

### Continuations Rule
Continuations are allowed more broadly than starts:
- If **only** the start is blocked due to Kaal overlap (not Vela), the time may still be **NEUTRAL** for continuations, *provided the block is otherwise not bad and score is not too low*.

---

## Optional Personal Scoring Model

### Natal Parameters (Fixed)
These are treated as constant inputs:

- **Janma Nakshatra:** `Pushya`
- **Janma Moon (Rashi):** `Karka` (Cancer)

### Score Components
If enabled, score is computed as:

TotalScore = ChoghadiyaPoints + TaraPoints + ChandraPoints


#### A) Choghadiya Base Points
Recommended mapping:

- `Amrita`: +3
- `Shubha`: +2
- `Labha`: +2
- `Chara`: +1
- `Udvega`: -1
- `Roga`: -2
- `Kala`: -2

#### B) Tara Bala Points
Compute based on nakshatra distance from Janma Nakshatra (Pushya) in 27-cycle:

1. Index nakshatras 1..27 (Ashwini=1 … Revati=27)
2. `d = (transit_index - janma_index) mod 27` (0..26)
3. `n = d + 1` (1..27)
4. `tara = ((n - 1) mod 9) + 1` (1..9)

Map tara number to points (tunable, but these are defaults):

- 1 (Janma): -2
- 2 (Sampat): +2
- 3 (Vipat): -2
- 4 (Kshema): +1
- 5 (Pratyari): -1
- 6 (Sadhaka): +2
- 7 (Naidhana): -3
- 8 (Mitra): +1
- 9 (Ati Mitra): +2

#### C) Chandra Bala Points
Compute house count from Janma Moon sign (Karka/Cancer) to transit Moon sign:

- `house = ((transit_rashi - janma_rashi) mod 12) + 1`

Points:
- if house ∈ {1, 3, 6, 7, 10, 11}: +1
- if house == 8: -2
- else: -1

---

## Bucket Assignment (GOOD / NEUTRAL / AVOID)

The exact thresholds are configurable but the current intended behavior:

### GOOD
- `StartAllowed == True`
- and `TotalScore >= +3`

### NEUTRAL
- either:
  - `StartAllowed == False` and `TotalScore >= +2` (good for continuation, but not starts)
  - OR `StartAllowed == True` and `TotalScore in {0, 1, 2}`

### AVOID
- everything else
- plus any `Vela` hard-avoid
- plus cases where continuation is not allowed (bad choghadiya + low score)

**Important:** Each Choghadiya block must be placed in exactly **one** calendar.

---

## Implementation Notes

### Time Parsing
Drik time formats may include:
- `"10:44 AM to 12:46 PM"`
- `"10:44 PM to 12:46 AM, Jan 02"` - This should include in next calendar day.
- with optional Vela tag at the end:
  - `"02:41 PM to 03:39 PM Kaal Vela"`

A robust parser must:
- parse `start` as base date, unless a next-day suffix exists and the start time is after midnight (AM), then use the suffix date
- parse `end` as base date unless suffix indicates otherwise
- tolerate optional whitespace around the comma in date suffixes (e.g., `"AM , Jan 02"`)
- if `end <= start`, roll `end` +1 day as a safety fallback
- keep timezone `Europe/Amsterdam`

### Dealing with "Image:" tokens
Sometimes Drik includes icon tokens that can appear adjacent to time text:
- Example: `"... to ...PMImage: Rahu Kalam"`

Always sanitize tokens by removing anything after `"Image:"`.

### Caching
Drik is an external website.
To avoid excessive requests:
- Use a local HTML cache keyed by URL + params.
- Allow a `--refresh` flag to refetch pages.

---

## Usage

### Install dependencies
pip install requests beautifulsoup4


### Run (example)
python calendar.py --start 2026-01-01 --days 14 --outdir out_ics


Outputs:
- `out_ics/choghadiya_good.ics`
- `out_ics/choghadiya_neutral.ics`
- `out_ics/choghadiya_avoid.ics`

---

## Validation Checklist (Must Pass)

For a test run `--start 2026-01-01 --days 14`:

1. **No IST used** (search code for `Asia/Kolkata`, `IST`, or conversions — must not exist).
2. Every datetime is `tzinfo=Europe/Amsterdam`.
3. Each day produces **16** Choghadiya blocks.
4. Kaals are only applied as **overlap tags** (no separate Kaal events).
5. Vela-tagged blocks always end up in **AVOID**.
6. Any block overlapping Rahu/Yamaganda/Gulikai has `StartAllowed = False`.
7. `.ics` files open correctly in Google Calendar.

---

## LLM Maintenance Instructions (MANDATORY)

Whenever you (an LLM or human) make a **major change** to this project, you MUST update this README to keep it accurate for future LLMs.

### Consider a change “major” if it affects:
- parsing strategy / regex
- scoring rules / thresholds
- bucketing logic
- time handling / timezone output
- data sources (URLs, page sections, geoname-id)
- how continuations vs starts are determined
- how moon/nakshatra timelines are parsed or used

### Update checklist for major changes
When you change code significantly, update these sections:

- **Data Sources**: add/modify URLs or parsing details
- **Core Logic**: explain any new parsing/segmentation approach
- **Scoring Model**: update formulas, weights, natal assumptions
- **Bucket Assignment**: update thresholds/rules
- **Validation Checklist**: add new expected invariants

### Document the change
Add a new entry to a "Changelog" section at the bottom of this README with:
- date of change
- what changed
- why it changed
- how to validate

---

## Changelog
- 2026-01-02: Fix choghadiya time parsing to capture date suffixes with whitespace and shift after-midnight start times to the suffix date; this prevents next-day night blocks from being assigned to the base date; validate by generating for a date like `2026-01-22` and confirming the `12:52 AM` block is dated `Jan 23` in the `.ics` output.

- _No entries yet._ Add entries here whenever major changes occur.

import asyncio
import csv
import json
import math
import os
import random
import re
import sys
import time
import aiohttp
from playwright.async_api import async_playwright, Error as PWError, TimeoutError as PWTimeout

FIRST = 0            # default start when there's no progress.txt
LAST = 0         # last ID to scrape (included), change this
START = None         # set a number to force a start, e.g. START = 1200

DEFAULT = "N/A"      # written when a field is missing or empty (use "" for blank cells)

CSV_PATH = "ASN Dataset v8.csv"      # new file: columns changed
PROGRESS_PATH = "progress.txt"
GEO_CACHE_PATH = "geocache.json"     # remembers every lookup so repeats are instant
GEO_VERSION = "2"                    # bump this whenever you change location_variants(); old cache is then ignored
FIELDS = ["Date", "Time", "Time note", "Type", "Location", "Country", "Lat", "Lng",
          "Fatalities", "Occupants", "Other fatalities", "Aircraft damage",
          "Category", "Phase", "Nature", "Probable Cause", "Owner/operator",
          "Registration", "MSN", "Year of manufacture", "Total airframe hrs",
          "Engine model", "Departure airport", "Destination airport",
          "Investigating agency", "Cause"]

HEADLESS = False
BLOCK_RESOURCES = True
DELAY = 0.5
MAX_RETRIES = 4
MAX_IN_A_ROW = 5             # real failures in a row -> stop
MAX_MISSING_IN_A_ROW = 500   # missing pages in a row -> stop (raise if your gaps are bigger)

# Nominatim's policy: max 1 request/second and an identifiable User-Agent.
# PUT YOUR REAL EMAIL HERE, a placeholder gets blocked (HTTP 403).
GEO_URL = "https://nominatim.openstreetmap.org/search"
GEO_HEADERS = {"User-Agent": "ASN-dataset-scraper/1.0 (alyelattar1@gmail.com)"}
GEO_MIN_GAP = 1.1            # seconds between Nominatim requests

# ASN country names that Nominatim doesn't recognize -> names it does
COUNTRY_FIX = {
    "Congo (Democratic Republic)": "Democratic Republic of the Congo",
    "Congo (Republic)": "Republic of the Congo",
}

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
FULL_DATE_RE = re.compile(rf"(\d{{1,2}}\s+(?:{MONTHS})\s+\d{{4}})", re.I)    # 30 August 2004
PARTIAL_DATE_RE = re.compile(rf"((?:{MONTHS})\s+\d{{4}}|\d{{4}})\s*$", re.I)  # August 2004 / 2004

# ---------------------------------------------------------------------------
# CAUSE CLASSIFIER
# Each category has (regex, weight) rules. Every rule that matches adds its
# weight to that category; the highest total wins. Order matters only for ties
# (earlier = wins), so specific/severe causes come first and the generic
# "Pilot Error" comes last. Text from the Probable Cause field counts double.
# ---------------------------------------------------------------------------
MIN_SCORE = 2        # below this -> "Undetermined"

_RAW_RULES = {
    "Terrorist Attack / Hijacking": [
        (r"terroris|hijack|hostage|stormed the cockpit|took control of the (aircraft|plane|cockpit)", 5),
    ],
    "Sabotage / Bomb": [
        (r"\bbomb\b|\bbombing\b|explosive|detonat|sabotage|\bIED\b|grenade", 5),
    ],
    "Military Action / Shot Down": [
        (r"shot down|missile|surface-to-air|anti-aircraft|airstrike|air strike|rocket attack|shelling|shelled|downed by|hit by (a )?(rocket|missile)|military (strike|attack)", 5),
    ],
    "Deliberate Act / Suicide": [
        (r"deliberate(ly)?|intentional(ly)?|suicide|murder|stole the (aircraft|plane|helicopter)|stolen (aircraft|plane|helicopter)", 3),
    ],
    "Bird / Wildlife Strike": [
        (r"bird ?strike|birds? (ingest|strike)|ingested (a )?bird|flock of|geese|wildlife strike|collided with (a )?(deer|animal|bird|cow|dog)|struck (a )?(deer|animal|bird|cow|dog)", 4),
    ],
    "Mid-air / Ground Collision": [
        (r"mid-?air|collided with (another|a) (aircraft|airplane|plane|helicopter|glider)|collision with (another|a) (aircraft|airplane|plane|helicopter|glider)|struck by (another|a) (aircraft|plane)", 4),
    ],
    "Air Traffic Control Error": [
        (r"(ATC|controllers?|air traffic control(ler)?s?) (error|mistake)|runway incursion|miscommunication|misunderstand(ing)?", 4),
    ],
    "Maintenance Error": [
        (r"maintenance (error|deficien\w+|issue|problem)|improperly (maintained|installed|repaired|secured)|inadequate maintenance|incorrectly (installed|assembled)|not properly (secured|maintained)|overdue (for )?inspection", 4),
    ],
    "Fuel Exhaustion / Starvation": [
        (r"fuel (exhaustion|starvation|depletion|contaminat\w+|exhausted)|out of fuel|(ran|run|running) (out of|low on) fuel|insufficient fuel", 4),
    ],
    "Engine Failure": [
        (r"engine (failure|failed|quit|stopped|stoppage|malfunction|trouble|problem|flame-?out|surge)", 3),
        (r"(lost|loss of|partial loss of) (engine )?power|flame-?out|power ?plant failure|propeller (failure|separat\w+)", 3),
    ],
    "Landing Gear Failure": [
        (r"(landing gear|undercarriage|nose ?gear|main gear|gear) (collapse\w*|failure|failed)|failed to (extend|lower)|did not (extend|lower)", 4),
    ],
    "Fire / Smoke Onboard": [
        (r"in-?flight fire|fire (broke out|started) (in|on)|smoke in the (cockpit|cabin)|smoke (filled|entered)|cargo (hold )?fire|cabin fire|cockpit fire|electrical fire|battery fire|lithium", 4),
    ],
    "Mechanical / Structural Failure": [
        (r"structural failure|in-?flight break-?up|broke apart|(wing|tail) (separat\w+|failed|came off)|fatigue crack|corrosion|metal fatigue", 3),
        (r"(hydraulic|flight control|control system|elevator|aileron|rudder|trim|electrical) (failure|malfunction|jam\w*|problem|fault)", 3),
        (r"(mechanical|technical) (failure|problem|issue|malfunction|fault)|(tail|main) rotor (failure|separat\w+)|gearbox|decompression", 3),
    ],
    # Every weather pattern is wrapped in \b...\b. Without it, "gust" matched
    # "August", "icing" matched "servicing", "hail" matched "Chailey", and so on.
    "Weather Conditions": [
        (r"\b(?:thunderstorms?|microbursts?|wind ?shear|icing|ice accretion|hail|tornado(?:es)?|cyclones?|hurricanes?|typhoons?|lightning|volcanic ash|severe turbulence|blizzards?)\b", 4),
        (r"\b(?:fog|low visibility|poor visibility|reduced visibility|heavy rain|downpour|snow(?:fall|storm)?|crosswinds?|gusts?|gusty|strong winds?|IMC|instrument meteorological conditions|haze|storms?|turbulence|sandstorm|dust storm|whiteout|wet runway|hydroplan\w*|aquaplan\w*)\b", 2),
        (r"\b(?:adverse|bad|poor|inclement) weather\b|\bweather conditions\b", 3),
    ],
    "Medical Incapacitation": [
        (r"incapacitat\w+|heart attack|hypoxia|carbon monoxide|CO poisoning|(pilot|captain|co-?pilot) (died|passed out|was unresponsive|became unresponsive|lost consciousness)", 4),
    ],
    "Overloading / Weight & Balance": [
        (r"overload\w*|over-?weight|exceed(ed|ing)? (the )?(maximum|max|weight|capacity)|center of gravity|weight and balance|improperly loaded|cargo shift\w*|load shift\w*", 4),
    ],
    "External Interference": [
        (r"\bdrones?\b|\bUAVs?\b|unmanned aerial|\blasers?\b|foreign object|\bFOD\b|debris|fireworks|bullets?|gunshots?|small arms|unlawful interference|balloon|\bkite\b", 3),
        (r"(collided with|struck|hit) (a |the )?(vehicle|truck|car|fence|person)|vehicle (on|crossed|entered) the runway", 3),
    ],
    "Controlled Flight Into Terrain": [
        (r"\bCFIT\b|controlled flight into terrain|flew into (the )?(terrain|mountain|ground|water|hill|trees|sea|lake)|GPWS|terrain (warning|awareness)|wire strike|power ?lines?", 4),
        (r"(struck|hit|crashed into) (a |the )?(mountain|mountainous terrain|hill|hillside|trees|wires?|ridge|terrain|antenna|tower)", 4),
    ],
    "Ground Handling / Taxi Accident": [
        (r"taxi(ing|ed)? (accident|collision)|while taxi(ing|ed)|pushback|being towed|ground (handling|crew|collision)|propeller strike|struck (the )?(jet ?bridge|hangar|building)|wing-?tip (struck|hit|clipped)", 3),
    ],
    "Pilot Error": [
        (r"pilot error|crew error|human error|misjudg\w+|inadvertent\w*|failed to (maintain|follow|monitor|extend|see|avoid)|continued (the )?(approach|flight|takeoff)|unstabili[sz]ed approach|too (high|low|fast|slow)", 3),
        (r"loss of control|lost control|\bstall(ed)?\b|\bspin\b|spatial disorientation|disorientat\w+|dynamic rollover|settling with power|brownout|loss of tail rotor effectiveness", 3),
        (r"(overshot|overran|overrun|veered off|ran off|went off|departed)( the)?( end of the)? runway|runway (excursion|overrun)|landed (long|short)|undershot|hard landing|bounced|porpois\w+|wrong runway|gear-?up|ground ?loop|without clearance", 2),
        (r"low[- ]level|aerobatic|\bbuzz\w*|intoxicat\w+|alcohol|inexperienc\w+", 2),
    ],
}

CAUSE_RULES = {c: [(re.compile(p, re.I), w) for p, w in rules] for c, rules in _RAW_RULES.items()}


def classify_cause(narrative, probable_cause=None):
    """Pick the best-scoring cause category. Probable Cause text counts double."""
    narrative = narrative if isinstance(narrative, str) else ""
    probable = probable_cause if isinstance(probable_cause, str) else ""
    if not narrative.strip() and not probable.strip():
        return DEFAULT

    totals = {c: 0 for c in CAUSE_RULES}
    for text, mult in ((narrative, 1), (probable, 2)):
        if not text:
            continue
        for cause, rules in CAUSE_RULES.items():
            totals[cause] += mult * sum(w for rx, w in rules if rx.search(text))

    best, best_score = "Undetermined", 0
    for cause, score in totals.items():          # strict > : earlier category wins ties
        if score > best_score:
            best, best_score = cause, score
    return best if best_score >= MIN_SCORE else "Undetermined"


# ---------------------------------------------------------------------------
# GEOCODING (Location + Country -> Lat, Lng, with distance/bearing offset)
# ---------------------------------------------------------------------------
BEARINGS = {
    "N": 0, "NNE": 22.5, "NE": 45, "ENE": 67.5, "E": 90, "ESE": 112.5, "SE": 135,
    "SSE": 157.5, "S": 180, "SSW": 202.5, "SW": 225, "WSW": 247.5, "W": 270,
    "WNW": 292.5, "NW": 315, "NNW": 337.5,
}
UNIT_KM = {"km": 1.0, "kms": 1.0, "mi": 1.609344, "miles": 1.609344, "nm": 1.852, "m": 0.001}

# "80 nm SSW of Kangerlussuaq", "50 km SW Clermont", "approx 6 mi S of Asmara"
OFFSET_RE = re.compile(
    r"^\s*(?:(?:near|approx\.?|about|c\.)\s+)?(\d+(?:\.\d+)?)\s*(km|kms|mi|miles|nm|m)\s+"
    r"([NSEW]{1,3})\b\s*(?:of|from)?\s+", re.I)


def parse_offset(place):
    """Returns (distance_km, bearing_degrees) if the text has distance + compass
    direction, else None. 'from X' with no direction can't be placed, so it's skipped."""
    m = OFFSET_RE.match(place or "")
    if not m:
        return None
    bearing = BEARINGS.get(m.group(3).upper())
    if bearing is None:                       # e.g. "NEW" matched the letters but isn't a direction
        return None
    return float(m.group(1)) * UNIT_KM[m.group(2).lower()], bearing


def offset_point(lat, lng, dist_km, bearing_deg):
    """Point dist_km away from (lat, lng) along bearing_deg (great-circle)."""
    d = dist_km / 6371.0
    b = math.radians(bearing_deg)
    p1, l1 = math.radians(lat), math.radians(lng)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1),
                         math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


def load_geo_cache():
    try:
        with open(GEO_CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        return {}
    if data.pop("_version", None) != GEO_VERSION:      # logic changed -> stale cache, start fresh
        return {}
    return data


def save_geo_cache(cache):
    with open(GEO_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump({**cache, "_version": GEO_VERSION}, f, ensure_ascii=False)


def location_variants(place, country):
    """Search strings to try, best first. Cleans up typical ASN wording like
    'Mariscal Lamar Int'l Airport (CUE)', 'near Madrid', '25 km N of Madrid'
    or '4.3 NM from Walton Airport (OPLH)'."""
    no_paren = re.sub(r"\s*\([^)]*\)", "", place).strip()          # drop "(CUE)"
    no_paren = re.sub(r"\s*,\s*,", ",", no_paren).strip()          # "Clermont,, QLD" -> "Clermont, QLD"
    cleaned = re.sub(r"\bInt'?l\.?\b", "International", no_paren, flags=re.I)
    cleaned = re.sub(r"^(near|off|over|approx\.?|about|close to|in the vicinity of)\s+",
                     "", cleaned, flags=re.I)
    cleaned = re.sub(r"^\d+(\.\d+)?\s*(km|kms|mi|miles|nm|m)\s*([NSEW]{1,3}\s+)?(of|from)?\s*",
                     "", cleaned, flags=re.I)                      # "25 km N of", "4.3 NM from"
    cleaned = cleaned.strip(" ,-")

    variants = []
    for q in (f"{cleaned}, {country}",
              f"{no_paren}, {country}",
              f"{cleaned.split(',')[0].strip()}, {country}"):      # first part only
        if q and q not in variants and not q.startswith(","):
            variants.append(q)
    return variants


async def _nominatim(state, query):
    """One throttled lookup. Returns [lat, lng], [] (not found), or None (error).
    Errors are printed so they never fail silently."""
    wait = GEO_MIN_GAP - (time.monotonic() - state["last_geo"])
    if wait > 0:
        await asyncio.sleep(wait)
    state["last_geo"] = time.monotonic()

    try:
        async with state["http"].get(
            GEO_URL, params={"q": query, "format": "jsonv2", "limit": 1}
        ) as r:
            if r.status != 200:
                body = (await r.text())[:150].replace("\n", " ")
                print(f"geocoder HTTP {r.status} for {query!r}: {body}")
                if r.status in (429, 503):
                    await asyncio.sleep(30)
                return None
            data = await r.json(content_type=None)
    except Exception as e:
        print(f"geocoder error for {query!r}: {type(e).__name__}: {e}")
        return None

    if not data:
        return []
    return [float(data[0]["lat"]), float(data[0]["lon"])]


async def geocode(state, place, country):
    """Returns (lat, lng) or (DEFAULT, DEFAULT). Needs BOTH location and country.
    The cache holds the reference place; the distance/bearing offset is applied after."""
    if place == DEFAULT or country == DEFAULT or not place or not country:
        return DEFAULT, DEFAULT

    country = COUNTRY_FIX.get(country, country)
    key = f"{place}|{country}".lower()
    cache = state["geo_cache"]

    if key in cache:
        base = cache[key]
    else:
        base = []
        for q in location_variants(place, country):
            result = await _nominatim(state, q)
            if result is None:              # network/rate-limit error: don't cache it
                return DEFAULT, DEFAULT
            if result:
                base = result
                break
        cache[key] = base or None           # genuinely not found: remember that too

    if not base:
        return DEFAULT, DEFAULT

    lat, lng = base
    off = parse_offset(place)
    if off:
        lat, lng = offset_point(lat, lng, *off)
    return round(lat, 5), round(lng, 5)


# ---------------------------------------------------------------------------
def get_start():
    # priority: command line > START variable > progress.txt > FIRST
    if len(sys.argv) > 1:
        return int(sys.argv[1])
    if START is not None:
        return START
    try:
        with open(PROGRESS_PATH) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return FIRST


def get_last():
    if len(sys.argv) > 2:
        return int(sys.argv[2])
    return LAST


def save_progress(num):
    with open(PROGRESS_PATH, "w") as f:
        f.write(str(num))


def date_from_title(title):
    """'Accident Cessna 421C HB-LRW, Monday 30 August 2004' -> '30 August 2004'.
    Falls back to partial dates ('August 2004', '2004'), then DEFAULT."""
    if not title or "," not in title:
        return DEFAULT
    tail = title.rsplit(",", 1)[1].strip()          # text after the LAST comma
    m = FULL_DATE_RE.search(tail)
    if m:
        return " ".join(m.group(1).split())
    m = PARTIAL_DATE_RE.search(tail)
    if m:
        return " ".join(m.group(1).split())
    return DEFAULT


def split_time(raw):
    """'c. 18:08' -> ('18:08', 'c. 18:08'); '18:30 LT' -> ('18:30', '18:30 LT');
    '07:43' -> ('07:43', N/A); 'day' -> (N/A, 'day'). Returns (Time, Time note)."""
    if not raw or raw == DEFAULT:
        return DEFAULT, DEFAULT
    raw = raw.strip()
    m = re.search(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)", raw)
    if not m:
        return DEFAULT, raw                          # 'day', 'afternoon', ...
    hhmm = f"{int(m.group(1)):02d}:{m.group(2)}"
    clean = re.fullmatch(r"\d{1,2}:\d{2}", raw) is not None
    return hhmm, (DEFAULT if clean else raw)


def parse_hours(raw):
    """'1,760 hours' -> '1760'. Anything without a number -> DEFAULT."""
    if not raw or raw == DEFAULT:
        return DEFAULT
    m = re.search(r"\d[\d,]*(?:\.\d+)?", raw)
    return m.group(0).replace(",", "") if m else DEFAULT


def split_location(raw):
    """'Place - Country' -> (place, country). Missing parts become DEFAULT."""
    if not raw:
        return DEFAULT, DEFAULT
    if " - " in raw:
        place, country = raw.rsplit(" - ", 1)
    elif "-" in raw:
        place, country = raw.rsplit("-", 1)
    else:
        return raw.strip() or DEFAULT, DEFAULT
    return place.strip() or DEFAULT, country.strip() or DEFAULT


def split_fatalities(raw):
    """'Fatalities: 1 / Occupants: 2' -> ('1', '2'). Missing parts become DEFAULT."""
    if not raw:
        return DEFAULT, DEFAULT
    fat = re.search(r"Fatalities:\s*([^/]*)", raw)
    occ = re.search(r"Occupants:\s*(.*)", raw)
    if not fat and not occ and ":" not in raw:
        return raw.strip() or DEFAULT, DEFAULT       # plain value, no labels
    fat_val = fat.group(1).strip() if fat else ""
    occ_val = occ.group(1).strip() if occ else ""
    return fat_val or DEFAULT, occ_val or DEFAULT


async def get_narrative(page):
    """Finds the 'Narrative:' caption and returns the text of its container,
    flattened to one line. Only used to work out the Cause, not saved to the CSV."""
    try:
        text = await page.evaluate("""() => {
            const cap = [...document.querySelectorAll('.caption')]
                .find(c => c.textContent.trim().toLowerCase().startsWith('narrative'));
            return cap ? cap.parentElement.innerText : '';
        }""")
    except Exception:
        return ""
    text = re.sub(r"^\s*Narrative:?", "", (text or "").strip(), flags=re.I)
    return " ".join(text.split())


async def block_junk(route):
    req = route.request
    if req.resource_type in ("font", "stylesheet", "media"):
        await route.abort()
    elif req.resource_type == "image" and "graphic" not in req.url:
        await route.abort()
    else:
        await route.continue_()


async def load_page(page, num, state):
    """True = page loaded, False = page doesn't exist (404/410/other 4xx)."""
    url = f"https://aviation-safety.net/wikibase/{num}"

    for attempt in range(MAX_RETRIES):
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            status = resp.status if resp else 0

            if status in (403, 429, 503):
                ra = resp.headers.get("retry-after", "")
                wait = int(ra) if ra.isdigit() else 30 * 2 ** attempt
                state["delay"] = min(state["delay"] * 1.5, 10)
                print(f"[{num}] HTTP {status}, waiting {wait}s, delay now {state['delay']:.1f}s")
                await asyncio.sleep(wait + random.random() * 3)
                continue

            return status < 400

        except (PWTimeout, PWError) as e:
            wait = 5 * 2 ** attempt
            print(f"[{num}] {type(e).__name__}, retrying in {wait}s")
            await asyncio.sleep(wait)

    raise RuntimeError(f"gave up on {num}")


async def save_plane_image(page, num, plane_type):
    images = page.locator(".desc img")
    if await images.count():
        image = images.first
        src = await image.get_attribute("src", timeout=5000)
        if src and "graphic" in src:
            name = re.sub(r"[^\w\-]+", "_", plane_type).strip("_") or "unknown"
            await image.screenshot(path=f"images/{name}_{num}.png", timeout=10000)


async def scrape(page, num, state):
    """Returns a row dict, or None if the page doesn't exist."""
    if not await load_page(page, num, state):
        return None                                   # 404 etc.

    # 200 but no data layout -> treat as a missing page too
    if await page.locator(".caption").count() == 0:
        return None

    raw_titles = await page.locator(".caption").all_inner_texts()
    raw_values = await page.locator(".desc").all_inner_texts()

    titles = [t.replace(":", "").strip() for t in raw_titles][2:-1]
    if len(titles) < 2:
        return None                                   # layout too short, nothing to parse

    values = [v.strip() for v in raw_values]
    attributes = {titles[0]: titles[1]} | dict(zip(titles, values))

    # Date comes from the <title>, overriding whatever the captions produced
    attributes["Date"] = date_from_title(await page.title())

    # Narrative is used to classify the Cause, but it isn't in FIELDS so it's not saved
    narrative = await get_narrative(page)
    attributes["Cause"] = classify_cause(narrative, attributes.get("Probable Cause"))

    # Clean time and airframe hours
    attributes["Time"], attributes["Time note"] = split_time(attributes.get("Time"))
    attributes["Total airframe hrs"] = parse_hours(attributes.get("Total airframe hrs"))

    # Split the packed fields (safe if they're missing)
    attributes["Fatalities"], attributes["Occupants"] = split_fatalities(attributes.get("Fatalities"))
    attributes["Location"], attributes["Country"] = split_location(attributes.get("Location"))

    # Coordinates from Location + Country. A geocoder problem never costs you the row.
    try:
        attributes["Lat"], attributes["Lng"] = await geocode(
            state, attributes["Location"], attributes["Country"])
    except Exception as e:
        print(f"[{num}] geocode skipped: {type(e).__name__}: {e}")
        attributes["Lat"], attributes["Lng"] = DEFAULT, DEFAULT

    # try:
    #     await save_plane_image(page, num, attributes.get("Type") or str(num))
    # except Exception as e:
    #     print(f"[{num}] image skipped: {e}")

    # missing OR empty -> DEFAULT
    return {k: (attributes.get(k) or DEFAULT) for k in FIELDS}


async def main():
    start, last = get_start(), get_last()
    end = last + 1                       # range() stops before this, so +1 includes LAST

    if start >= end:
        print(f"Nothing to do: start {start} is past last ID {last}. "
              f"Delete progress.txt or pass a start number.")
        return
    print(f"Scraping IDs {start} to {last}")

    if "your_email@example.com" in GEO_HEADERS["User-Agent"]:
        print("WARNING: GEO_HEADERS still has the placeholder email. "
              "Nominatim will likely block it (HTTP 403), so Lat/Lng will be N/A.")

    os.makedirs("images", exist_ok=True)
    new_file = not os.path.exists(CSV_PATH)
    state = {"delay": DELAY, "geo_cache": load_geo_cache(), "last_geo": 0.0, "http": None}
    failed, missing = [], []
    in_a_row, missing_in_a_row = 0, 0
    streak_start = None          # first ID of current failure streak
    missing_start = None         # first ID of current missing streak
    resume_at = start

    async with async_playwright() as p, aiohttp.ClientSession(
        headers=GEO_HEADERS, timeout=aiohttp.ClientTimeout(total=15)
    ) as http:
        state["http"] = http
        browser = await p.chromium.launch(headless=HEADLESS)
        try:
            context = await browser.new_context()
            if BLOCK_RESOURCES:
                await context.route("**/*", block_junk)
            page = await context.new_page()

            with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=FIELDS)
                if new_file:
                    writer.writeheader()

                for num in range(start, end):
                    if streak_start is not None:
                        resume_at = streak_start
                    elif missing_start is not None:
                        resume_at = missing_start
                    else:
                        resume_at = num
                    save_progress(resume_at)

                    try:
                        row = await scrape(page, num, state)
                        in_a_row, streak_start = 0, None     # got a real response

                        if row:
                            writer.writerow(row)
                            f.flush()
                            missing_in_a_row, missing_start = 0, None
                        else:
                            missing.append(num)
                            if missing_in_a_row == 0:
                                missing_start = num
                            missing_in_a_row += 1
                            print(f"[{num}] doesn't exist, skipping")
                            if missing_in_a_row >= MAX_MISSING_IN_A_ROW:
                                print(f"{missing_in_a_row} missing pages in a row, "
                                      f"possible block. Resume point saved: {missing_start}")
                                resume_at = missing_start
                                break

                    except Exception as e:
                        failed.append(num)
                        if in_a_row == 0:
                            streak_start = num
                        in_a_row += 1
                        print(f"[{num}] FAILED: {type(e).__name__}: {e}")
                        if in_a_row >= MAX_IN_A_ROW:
                            print(f"Too many failures, probably blocked. Resume point saved: {streak_start}")
                            resume_at = streak_start
                            break

                    await asyncio.sleep(state["delay"] + random.random() * 0.3)
                else:
                    resume_at = end      # finished the whole range
        finally:
            save_progress(resume_at)
            save_geo_cache(state["geo_cache"])
            await browser.close()
            if failed:
                with open("failed_ids.txt", "w") as ff:
                    ff.write("\n".join(map(str, failed)))
                print(f"{len(failed)} failed, saved to failed_ids.txt")
            if missing:
                with open("missing_ids.txt", "w") as mf:
                    mf.write("\n".join(map(str, missing)))
                print(f"{len(missing)} pages didn't exist, saved to missing_ids.txt")


if __name__ == "__main__":
    asyncio.run(main())
"""
Egypt Film Radar
----------------
https://github.com/YoussefElnaka/egypt-film-radar

Sends an email every two weeks (by default) with two sections:

  1. Performing well in theaters: Egyptian movies now in Egyptian cinemas
     whose ElCinema rating reached MIN_RATING (with at least MIN_VOTES votes).
     Each movie keeps getting re-checked for RECHECK_DAYS after the script
     first sees it in theaters, because ratings move after release.

  2. Now streaming: movies that qualified for section 1 (now or in the past)
     and just became available on a streaming service anywhere in the world.
     Three sources are checked:
       - Yango Play, directly (its public list of titles)
       - TMDB, which carries JustWatch's data for Netflix, Shahid, OSN+,
         STARZPLAY, TOD, Prime Video and more, in every country
       - ElCinema's VOD Guide (the only source that sometimes has WATCH IT)

This product uses the TMDB API but is not endorsed or certified by TMDB.
Streaming availability data from TMDB is provided by JustWatch.

Settings live in the .env file. Everything the script remembers lives in
/app/data/state.json.
The Yango title index is a cache kept in /app/data/yango_index.json.
"""

import difflib
import html
import json
import os
import random
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def load_env_file(path):
    """When run without Docker, read settings from a .env file next to the
    script. Anything already set in the environment wins."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]                 # KEY="value" -> value
            else:
                value = value.split(" #", 1)[0].strip()  # KEY=value  # comment -> value
            os.environ.setdefault(key.strip(), value)


load_env_file(os.path.join(SCRIPT_DIR, ".env"))

def env_number(name, default, kind=float):
    """Read a number setting. Empty or invalid values fall back to the default
    (with a note in the log) instead of crashing."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return kind(raw)
    except ValueError:
        print(f"Note: {name}={raw!r} in .env isn't a valid number, using {default} instead.")
        return default


VERSION = "1.1.0"
PROJECT_URL = "https://github.com/YoussefElnaka/egypt-film-radar"

EMAIL_ADDRESS = os.environ.get("EMAIL_ADDRESS")
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip() or "smtp.gmail.com"
SMTP_PORT = env_number("SMTP_PORT", 587, int)
SMTP_USER = os.environ.get("SMTP_USER", "").strip() or EMAIL_ADDRESS  # Usually the same as EMAIL_ADDRESS
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "").strip()
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
TO_EMAIL = os.environ.get("TO_EMAIL")

# These come from the .env file. The numbers here are only used if a line
# is missing from .env.
MIN_RATING = env_number("MIN_RATING", 6.0)                          # Rating needed to qualify
MIN_VOTES = env_number("MIN_VOTES", 20, int)                        # Ignore ratings with fewer votes
RECHECK_WEEKS = env_number("RECHECK_WEEKS", 4, int)                 # Keep re-checking ratings this long
STREAMING_WATCH_MONTHS = env_number("STREAMING_WATCH_MONTHS", 12, int)  # Give up on streaming after this

RUN_EVERY_DAYS = env_number("RUN_EVERY_DAYS", 14, int)  # Send at most this often (task runs weekly)
SHOW_ARABIC_TITLES = os.environ.get("SHOW_ARABIC_TITLES", "false").strip().lower() in ("true", "yes", "1", "on")

RECHECK_DAYS = RECHECK_WEEKS * 7 + 2  # +2 days of slack so the last scheduled run still counts
STREAMING_WATCH_DAYS = int(STREAMING_WATCH_MONTHS * 30.5)
MAX_MOVIE_AGE_YEARS = 1  # Movies older than this that are back in cinemas count as re-releases

# TEST_MODE=1 sends the email with [TEST] in the subject and does NOT save
# anything, so you can run it as many times as you like.
# DRY_RUN=1 doesn't send anything; it writes the email to data/preview.html.
TEST_MODE = os.environ.get("TEST_MODE") == "1"
DRY_RUN = os.environ.get("DRY_RUN") == "1"

DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(SCRIPT_DIR, "data")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
PREVIEW_FILE = os.path.join(DATA_DIR, "preview.html")
YANGO_INDEX_FILE = os.path.join(DATA_DIR, "yango_index.json")

BASE = "https://elcinema.com"
# Two views of "Now Playing in Egypt": the full list, and the Arabic-language
# filter (which sometimes includes titles the full list leaves out).
NOW_PLAYING_URLS = [
    BASE + "/en/now/eg/?page={page}",
    BASE + "/en/now/eg/?language=ar&page={page}",
]
WORK_URL = BASE + "/en/work/{id}/"
ARABIC_WORK_URL = BASE + "/work/{id}/"
# Identify ourselves honestly to the sites we read, with a link to this project.
HEADERS = {"User-Agent": f"egypt-film-radar/{VERSION} (+{PROJECT_URL})"}

YANGO_SITEMAP = "https://play.yango.com/sitemap.xml"
TMDB_BASE = "https://api.themoviedb.org/3"

# Nicer display names for ElCinema's platform codes. Anything not listed
# here is shown with underscores turned into spaces, e.g. "sling_tv" -> "Sling Tv".
PLATFORM_NAMES = {
    "netflix": "Netflix",
    "shahid": "Shahid",
    "watchit": "WATCH IT",
    "yango_play": "Yango Play",
    "youtube": "YouTube",
    "osn_plus": "OSN+",
    "starzplay": "STARZPLAY",
    "tod": "TOD",
    "disney_plus": "Disney+",
    "amazon_prime_video": "Prime Video",
    "apple_tv": "Apple TV",
    "sling_tv": "Sling TV",
    "jawwy_tv": "Jawwy TV",
    "viu": "Viu",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def today():
    return datetime.now().strftime("%Y-%m-%d")


def days_since(date_str):
    return (datetime.now() - datetime.strptime(date_str, "%Y-%m-%d")).days


def polite_pause():
    time.sleep(random.uniform(1.5, 3.0))


def hide_secrets(text):
    """Keep the TMDB key out of logs (error messages can include the full URL)."""
    text = str(text)
    return text.replace(TMDB_API_KEY, "***") if TMDB_API_KEY else text


def fetch(url):
    """Download a page, retrying twice on failure. Returns HTML text."""
    last_error = None
    for attempt in range(3):
        try:
            res = requests.get(url, headers=HEADERS, timeout=30)
            if res.status_code == 404:
                raise RuntimeError("page not found (404)")  # Retrying won't help
            res.raise_for_status()
            return res.text
        except Exception as e:
            last_error = e
            if "404" in str(e):
                break
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Could not load {url}: {last_error}")


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"candidates": {}, "watchlist": {}, "ignored": {}, "done": {}, "last_run": None}


def save_state(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)  # Swap in one go so a crash can't corrupt it


def is_known(state, work_id):
    return any(work_id in state[k] for k in ("candidates", "watchlist", "ignored", "done"))


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------
def get_now_playing():
    """Return {work_id: title} for everything in Egyptian cinemas right now."""
    movies = {}
    for url in NOW_PLAYING_URLS:
        for page in range(1, 11):
            soup = BeautifulSoup(fetch(url.format(page=page)), "html.parser")
            found_new = False
            for a in soup.find_all("a", href=True):
                m = re.match(r"^/en/work/(\d+)/?$", a["href"])
                title = a.get_text(strip=True)
                if m and title and m.group(1) not in movies:
                    movies[m.group(1)] = title
                    found_new = True
            polite_pause()
            if not found_new:
                break
    return movies


def get_movie_details(work_id):
    """Read one movie's ElCinema page and pull out everything we need."""
    soup = BeautifulSoup(fetch(WORK_URL.format(id=work_id)), "html.parser")
    info = {"id": work_id, "link": WORK_URL.format(id=work_id)}

    # Title and production year, from the heading, e.g. "Asad (2026) Lion"
    h1 = soup.find("h1")
    h1_text = h1.get_text(" ", strip=True) if h1 else ""
    year_match = re.search(r"\((\d{4})\)", h1_text)
    info["year"] = int(year_match.group(1)) if year_match else None
    # Title is everything before "(year)", so "El Set Lamma (Veto) (2026)" keeps "(Veto)"
    if year_match:
        info["title"] = h1_text[:year_match.start()].strip() or None
    else:
        info["title"] = h1_text.split("(")[0].strip() or None

    # Rating and vote count. The rating link's tooltip reads
    # "التقييم : 7.1 - عدد 80 صوت" (Rating: 7.1 - 80 votes)
    info["rating"], info["votes"] = 0.0, 0
    stats = soup.find("a", href=f"/en/work/{work_id}/stats")
    if stats and stats.get("title"):
        # Remove thousands separators first, so "1,234 votes" isn't read as 1
        text = re.sub(r"(?<=\d)[,\u066C](?=\d{3})", "", stats["title"])
        numbers = re.findall(r"\d+(?:\.\d+)?", text)
        if len(numbers) >= 2:
            info["rating"], info["votes"] = float(numbers[0]), int(numbers[1])

    # Countries of production
    countries = {
        a["href"].rstrip("/").split("/")[-1]
        for a in soup.find_all("a", href=re.compile(r"/en/index/work/country/[a-z]{2}/?$"))
    }
    info["egyptian"] = "eg" in countries
    info["has_countries"] = bool(countries)  # New ElCinema pages are sometimes incomplete

    # Genres, e.g. ["Action", "Thriller"]. Only the first two, to keep the email tidy.
    genres = []
    for a in soup.find_all("a", href=re.compile(r"/en/index/work/genre/")):
        g = a.get_text(strip=True)
        if g and g not in genres:
            genres.append(g)
    info["genres"] = genres[:2]

    # Poster
    og_img = soup.find("meta", property="og:image")
    info["poster"] = og_img["content"] if og_img and og_img.get("content") else None

    # Streaming links for THIS movie only. ElCinema tags each streaming link
    # with the movie's ID (class "cta-work-<id>") and the platform
    # (class "cta-vod-<platform>-<number>"). Recommendations of other movies
    # on the same page carry their own IDs, so they're skipped.
    platforms = {}
    for a in soup.find_all("a", class_=f"cta-work-{work_id}", href=True):
        code = None
        for cls in a.get("class", []):
            m = re.match(r"^cta-vod-(?!slot-)(.+)-\d+$", cls)
            if m:
                code = m.group(1)
                break
        if not code:
            m = re.search(r"Online on (\S+)$", a.get("title", ""))
            code = m.group(1) if m else "streaming"
        name = PLATFORM_NAMES.get(code, code.replace("_", " ").title())
        platforms.setdefault(name, {"url": a["href"], "where": ""})
    info["platforms"] = platforms

    # IMDb ID, if ElCinema links to IMDb (helps find the movie on TMDB)
    imdb = soup.find("a", href=re.compile(r"imdb\.com/title/tt\d+"))
    info["imdb"] = re.search(r"tt\d+", imdb["href"]).group(0) if imdb else None

    return info


def get_release_date(work_id):
    """Egyptian release date from ElCinema's per-country list, e.g. "27 May 2026".
    Prefers Egypt's regular release over a premiere screening. If the movie
    hasn't been released in Egypt, shows the first country instead."""
    soup = BeautifulSoup(fetch(WORK_URL.format(id=work_id) + "released"), "html.parser")
    rows = []
    table = soup.find("table")
    for tr in (table.find_all("tr") if table else []):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(cells) >= 2 and re.match(r"\d{1,2} \w+ \d{4}$", cells[1]):
            premiere = len(cells) > 2 and cells[2].lower().startswith("yes")
            rows.append((cells[0], cells[1], premiere))
    for want_premiere in (False, True):
        for country, date, premiere in rows:
            if country == "Egypt" and premiere == want_premiere:
                return short_date(date)
    if rows:
        return f"{short_date(rows[0][1])} ({rows[0][0]})"
    return None


def short_date(date):
    """ "27 May 2026" -> "27 May 2026", "12 August 2026" -> "12 Aug 2026" """
    try:
        d = datetime.strptime(date, "%d %B %Y")
        return f"{d.day} {d:%b %Y}"  # Works on every OS (unlike "%-d")
    except ValueError:
        return date


def add_extras(info, item=None):
    """Release date and Arabic title for the email. Looked up once per movie
    and remembered in the watchlist entry (item) so later runs skip it."""
    item = item if item is not None else {}
    for key, lookup in (("release_date", get_release_date), ("arabic_title", get_arabic_title)):
        if not item.get(key):
            try:
                item[key] = lookup(info["id"])
            except Exception as e:
                print(f"    Couldn't get {key.replace('_', ' ')} for {info['title']}: {e}")
        info[key] = item.get(key)


def get_arabic_title(work_id):
    """ElCinema's Arabic page heading, e.g. "برشامة (2026) Bershama" -> "برشامة"."""
    soup = BeautifulSoup(fetch(ARABIC_WORK_URL.format(id=work_id)), "html.parser")
    h1 = soup.find("h1")
    return h1.get_text(" ", strip=True).split("(")[0].strip() if h1 else None


def qualifies(info):
    return info["rating"] >= MIN_RATING and info["votes"] >= MIN_VOTES


def is_recent(info):
    return info["year"] is None or info["year"] >= datetime.now().year - MAX_MOVIE_AGE_YEARS


# ---------------------------------------------------------------------------
# Title matching
# ---------------------------------------------------------------------------
def norm_latin(t):
    """Make English spellings comparable: "El Shater" ~ "Al Shater"."""
    t = re.sub(r"\bel\b", "al", (t or "").lower()).replace("ou", "u")
    return re.sub(r"[^a-z0-9]", "", t)


def norm_arabic(t):
    """Make Arabic spellings comparable (ignore diacritics and alef/yaa/taa variants)."""
    t = re.sub(r"[\u064B-\u0652\u0640]", "", t or "")
    t = re.sub("[أإآ]", "ا", t).replace("ى", "ي").replace("ة", "ه")
    return re.sub(r"[^\u0621-\u064A0-9]", "", t)


def titles_match(a, b, norm):
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    return na == nb or difflib.SequenceMatcher(None, na, nb).ratio() >= 0.9


def years_close(a, b):
    return a is None or b is None or abs(a - b) <= 1


# ---------------------------------------------------------------------------
# Yango Play
# ---------------------------------------------------------------------------
# Yango publishes a sitemap listing every title page. We read each new page
# once, note its title/year/country, and keep that in yango_index.json so
# later runs only read pages that are new since last time.
def load_yango_index():
    if os.path.exists(YANGO_INDEX_FILE):
        with open(YANGO_INDEX_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_yango_index(index):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = YANGO_INDEX_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False)
    os.replace(tmp, YANGO_INDEX_FILE)


def update_yango_index(index):
    xml = fetch(YANGO_SITEMAP)
    cutoff = f"{datetime.now().year - 1}-01-01"  # Only pages updated since last January
    entries = re.findall(
        r"<loc>(https://play\.yango\.com/en/movies/film/\w+)</loc>\s*<lastmod>([\d-]+)</lastmod>", xml
    )
    if not entries:
        raise RuntimeError("Yango's title list was empty or changed format")
    new_urls = [u for u, lastmod in entries if lastmod >= cutoff and u not in index]
    note = " (first time, this takes 20-30 minutes)" if len(new_urls) > 200 else ""
    print(f"  {len(new_urls)} new Yango pages to read{note}")
    for i, url in enumerate(new_urls, 1):
        try:
            page = fetch(url)
        except Exception as e:
            print(f"    Skipping {url}: {e}")  # Not recorded, so it's retried next run
            continue
        # Movie pages describe themselves as "Watch Bershama (2026, Egypt) online ..."
        # Series pages say "Stream ..." instead, so they're recorded as None.
        m = re.search(r'<meta name="description" content="Watch (.*?) \((\d{4}), ([^)]*)\) online', page)
        index[url] = [html.unescape(m.group(1)), int(m.group(2)), html.unescape(m.group(3))] if m else None
        if i % 100 == 0:
            print(f"    {i}/{len(new_urls)} read...")
            save_yango_index(index)  # Save progress in case the run is interrupted
        time.sleep(random.uniform(0.8, 1.5))
    save_yango_index(index)


def find_on_yango(index, title, year):
    for url, entry in index.items():
        if entry and "Egypt" in entry[2] and years_close(year, entry[1]) \
                and titles_match(title, entry[0], norm_latin):
            return url
    return None


# ---------------------------------------------------------------------------
# TMDB (JustWatch data for Netflix, Shahid, OSN+, STARZPLAY, TOD, Prime...)
# ---------------------------------------------------------------------------
class TMDB:
    def __init__(self, api_key):
        self.api_key = api_key
        self.problem = None if api_key else (
            "TMDB_API_KEY is missing from .env, so Netflix, Shahid, OSN+ and similar "
            "services weren't checked. Only Yango Play and ElCinema were."
        )

    def get(self, path, **params):
        if self.problem:
            return None
        params["api_key"] = self.api_key
        res = requests.get(TMDB_BASE + path, params=params, headers=HEADERS, timeout=30)
        if res.status_code == 401:
            self.problem = ("TMDB rejected the API key in .env, so Netflix, Shahid, OSN+ and "
                            "similar services weren't checked. Check TMDB_API_KEY.")
            print("  " + self.problem)
            return None
        res.raise_for_status()
        return res.json()

    def find_movie_id(self, info, arabic_title):
        # 1. Exact match through IMDb, when ElCinema has the IMDb link
        if info.get("imdb"):
            data = self.get(f"/find/{info['imdb']}", external_source="imdb_id")
            if data and data.get("movie_results"):
                return data["movie_results"][0]["id"]
        # 2. Search by Arabic title, then English title, checking the year
        for query, norm in ((arabic_title, norm_arabic), (info.get("title"), norm_latin)):
            if not query:
                continue
            data = self.get("/search/movie", query=query, include_adult="false")
            for r in (data or {}).get("results", [])[:10]:
                year = int(r["release_date"][:4]) if r.get("release_date") else None
                if not years_close(info.get("year"), year):
                    continue
                if titles_match(query, r.get("original_title"), norm) or titles_match(query, r.get("title"), norm):
                    return r["id"]
        return None

    def streaming_services(self, tmdb_id):
        """All subscription/free services, in every country. Rent/buy is ignored."""
        data = self.get(f"/movie/{tmdb_id}/watch/providers")
        services = {}
        for country, offers in ((data or {}).get("results") or {}).items():
            for kind in ("flatrate", "free", "ads"):
                for p in offers.get(kind) or []:
                    name = p.get("provider_name", "Unknown")
                    if name.startswith("Netflix"):
                        name = "Netflix"  # Fold "Netflix Standard with Ads" etc. into one
                    entry = services.setdefault(name, {"countries": set(), "url": offers.get("link")})
                    entry["countries"].add(country)
                    if country == "EG" and offers.get("link"):
                        entry["url"] = offers["link"]
        return services


def describe_countries(codes):
    codes = sorted(codes, key=lambda c: (c != "EG", c))
    shown = ", ".join(codes[:3])
    return shown + (f" +{len(codes) - 3} more" if len(codes) > 3 else "")


# ---------------------------------------------------------------------------
# The main logic
# ---------------------------------------------------------------------------
def run_checks(state, yango_index, tmdb):
    theater_hits = []
    streaming_hits = []
    warnings = []

    # --- Step 1: what's in theaters right now ---
    print("Loading ElCinema's Now Playing list for Egypt...")
    try:
        now_playing = get_now_playing()
    except Exception as e:
        now_playing = {}
        warnings.append(f"Couldn't load ElCinema's Now Playing page ({e}).")
    print(f"  {len(now_playing)} movies in Egyptian cinemas.")
    if not now_playing and not warnings:
        warnings.append(
            "ElCinema's Now Playing page returned zero movies. That usually means "
            "the site changed its layout and the script needs updating."
        )

    # --- Step 2: add movies we haven't seen before as candidates ---
    for work_id, title in now_playing.items():
        if is_known(state, work_id):
            continue
        polite_pause()
        try:
            info = get_movie_details(work_id)
        except Exception as e:
            print(f"  Couldn't check {title}: {e}")
            continue
        if not info["has_countries"]:
            # Page doesn't say where it's from yet. Don't rule it out; look again next run.
            print(f"  {title}: ElCinema doesn't list its country yet, will check again next run")
        elif not info["egyptian"]:
            state["ignored"][work_id] = {"title": title, "reason": "not Egyptian"}
            print(f"  {title}: not Egyptian, skipping")
        elif not is_recent(info):
            state["ignored"][work_id] = {"title": title, "reason": f"re-release ({info['year']})"}
            print(f"  {title}: re-release from {info['year']}, skipping")
        else:
            state["candidates"][work_id] = {"title": title, "first_seen": today()}
            print(f"  {title}: new Egyptian release, added to candidates")

    # --- Step 3: re-check every candidate's rating ---
    print("Checking ratings of candidates...")
    for work_id, cand in list(state["candidates"].items()):
        polite_pause()
        try:
            info = get_movie_details(work_id)
        except Exception as e:
            print(f"  Couldn't check {cand['title']}: {e}")
            continue
        print(f"  {cand['title']}: {info['rating']} ({info['votes']} votes)")
        if qualifies(info):
            entry = {"title": cand["title"], "added": today()}
            add_extras(info, entry)
            theater_hits.append(info)
            del state["candidates"][work_id]
            state["watchlist"][work_id] = entry
        elif days_since(cand["first_seen"]) > RECHECK_DAYS:
            del state["candidates"][work_id]
            state["ignored"][work_id] = {"title": cand["title"], "reason": f"stayed below {MIN_RATING}"}
            print("    Re-check window over, dropping it.")

    # --- Step 4: look for streaming on the watchlist ---
    if state["watchlist"]:
        print("Updating the Yango Play title index...")
        try:
            update_yango_index(yango_index)
        except Exception as e:
            warnings.append(f"Couldn't read Yango Play's title list ({e}). Yango wasn't checked this time.")
            print("  " + warnings[-1])

    print("Checking the streaming watchlist...")
    for work_id, item in list(state["watchlist"].items()):
        if days_since(item["added"]) > STREAMING_WATCH_DAYS:
            del state["watchlist"][work_id]
            state["done"][work_id] = {"title": item["title"], "reason": "no streaming found in time"}
            print(f"  {item['title']}: no streaming found within {STREAMING_WATCH_MONTHS} months, giving up")
            continue
        polite_pause()
        try:
            info = get_movie_details(work_id)
        except Exception as e:
            print(f"  Couldn't check {item['title']}: {e}")
            continue

        # Keep the latest rating for the email's monitoring list
        item["rating"], item["votes"] = info["rating"], info["votes"]
        add_extras(info, item)

        # Source 1: ElCinema's own VOD Guide, already in info["platforms"]
        elcinema_result = ", ".join(info["platforms"]) or "none"

        # Source 2: Yango Play
        if not yango_index:
            yango_result = "not checked"
        else:
            yango_url = find_on_yango(yango_index, info["title"] or item["title"], info["year"])
            yango_result = "found" if yango_url else "none"
            if yango_url:
                info["platforms"].setdefault("Yango Play", {"url": yango_url, "where": "Middle East & North Africa"})

        # Source 3: TMDB. Find the movie once and remember its TMDB ID.
        tmdb_result = "not checked"
        try:
            if not item.get("tmdb_id") and not tmdb.problem:
                item["tmdb_id"] = tmdb.find_movie_id(info, info.get("arabic_title"))
            if item.get("tmdb_id") and not tmdb.problem:
                services = tmdb.streaming_services(item["tmdb_id"])
                tmdb_result = ", ".join(services) if services else "movie found, no services yet"
                for name, svc in services.items():
                    info["platforms"].setdefault(
                        name, {"url": svc["url"], "where": describe_countries(svc["countries"])})
            elif not tmdb.problem:
                tmdb_result = "movie not on TMDB yet"
        except Exception as e:
            tmdb_result = f"error ({hide_secrets(e)})"

        print(f"  {item['title']}: ElCinema {elcinema_result} | Yango Play {yango_result} | TMDB {tmdb_result}")
        if info["platforms"]:
            print(f"    -> STREAMING on {', '.join(info['platforms'])}")
            streaming_hits.append(info)
            del state["watchlist"][work_id]
            state["done"][work_id] = {"title": item["title"], "streaming_found": today()}
        else:
            print("    -> not streaming yet")

    if tmdb.problem:
        warnings.append(tmdb.problem)
    return theater_hits, streaming_hits, warnings


# ---------------------------------------------------------------------------
# The email
# ---------------------------------------------------------------------------
def movie_card(m, extra_html):
    e = html.escape
    title = e(m["title"] or "Untitled")
    arabic = ""
    # Only if it's actually in Arabic (some movies use their English name on the Arabic site too)
    if SHOW_ARABIC_TITLES and m.get("arabic_title") and re.search(r"[\u0600-\u06FF]", m["arabic_title"]):
        arabic = (f'<div dir="rtl" style="text-align:left;color:#7f8c8d;font-size:15px;margin-top:2px;">'
                  f'{e(m["arabic_title"])}</div>')
    poster = ""
    if m.get("poster"):
        poster = (
            f'<td width="110" valign="top" style="padding-right:15px;">'
            f'<a href="{e(m["link"])}"><img src="{e(m["poster"])}" alt="" width="100" '
            f'style="border-radius:5px;display:block;max-width:100px;"></a></td>'
        )
    return f"""
    <tr><td style="padding:12px 0;border-bottom:1px solid #eee;">
      <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
        {poster}
        <td valign="top">
          <a href="{e(m["link"])}" style="color:#2c3e50;text-decoration:none;font-size:18px;font-weight:bold;">{title}</a>
          {arabic}
          {extra_html}
        </td>
      </tr></table>
    </td></tr>"""


def section(heading, color, cards_html, empty_text):
    body = cards_html or f'<tr><td style="color:#7f8c8d;padding:10px 0;">{empty_text}</td></tr>'
    return f"""
    <h2 style="color:{color};font-size:20px;margin:25px 0 5px 0;border-bottom:2px solid {color};padding-bottom:6px;">{heading}</h2>
    <table width="100%" cellpadding="0" cellspacing="0" border="0">{body}</table>"""


def rating_html(m):
    small = 'style="margin:4px 0 0 0;color:#7f8c8d;font-size:13px;"'
    date = f'<p {small}>Released {html.escape(m["release_date"])}</p>' if m.get("release_date") else ""
    if m.get("genres"):
        date += f'<p {small}>{html.escape(" · ".join(m["genres"]))}</p>'
    if not m.get("votes"):
        return f'<p style="margin:8px 0 0 0;font-size:15px;color:#7f8c8d;">Not rated yet</p>{date}'
    return (f'<p style="margin:8px 0 0 0;font-size:15px;">'
            f'<span style="font-size:18px;font-weight:bold;color:#e67e22;">{m["rating"]} / 10</span>'
            f' <span style="color:#7f8c8d;font-size:13px;">from {m["votes"]:,} ratings</span></p>{date}')


def monitoring_html(watchlist):
    items = sorted(watchlist.items(), key=lambda kv: kv[1].get("rating") or 0, reverse=True)
    if not items:
        return '<p style="color:#95a5a6;font-size:13px;margin:6px 0;">Nothing being monitored right now.</p>'
    rows = ""
    for work_id, i in items:
        details = []
        if i.get("rating"):
            details.append(f'<span style="color:#e67e22;font-weight:bold;">{i["rating"]}</span>')
        if i.get("release_date"):
            details.append(html.escape(i["release_date"]))
        rows += (
            f'<tr><td style="padding:4px 0;border-bottom:1px solid #f2f2f2;font-size:13px;color:#2c3e50;">'
            f'&bull;&nbsp; <a href="{WORK_URL.format(id=work_id)}" style="color:#2c3e50;text-decoration:none;">'
            f'{html.escape(i["title"])}</a></td>'
            f'<td align="right" style="padding:4px 0;border-bottom:1px solid #f2f2f2;font-size:12px;'
            f'color:#95a5a6;white-space:nowrap;">{" &middot; ".join(details)}</td></tr>'
        )
    return f'<table width="100%" cellpadding="0" cellspacing="0" border="0">{rows}</table>'


def build_email(theater_hits, streaming_hits, warnings, watchlist):
    by_rating = lambda m: m["rating"]
    theater_hits = sorted(theater_hits, key=by_rating, reverse=True)
    streaming_hits = sorted(streaming_hits, key=by_rating, reverse=True)

    theater_cards = "".join(movie_card(m, rating_html(m)) for m in theater_hits)

    streaming_cards = ""
    for m in streaming_hits:
        buttons = "".join(
            f'<a href="{html.escape(p["url"] or m["link"])}" style="display:inline-block;margin:8px 8px 0 0;'
            f'padding:7px 12px;background:#27ae60;color:#fff;text-decoration:none;border-radius:5px;'
            f'font-size:13px;font-weight:bold;">{html.escape(name)}</a>'
            for name, p in m["platforms"].items()
        )
        where = " &middot; ".join(
            f"{html.escape(name)}: {html.escape(p['where'])}"
            for name, p in m["platforms"].items() if p.get("where")
        )
        where_html = f'<p style="margin:8px 0 0 0;color:#95a5a6;font-size:12px;">{where}</p>' if where else ""
        streaming_cards += movie_card(m, f"{rating_html(m)}<div>{buttons}</div>{where_html}")

    warning_html = "".join(
        f'<p style="background:#fdecea;color:#c0392b;padding:10px;border-radius:5px;font-size:14px;">'
        f"&#9888; {html.escape(w)}</p>"
        for w in warnings
    )

    body = f"""
    <html><body style="font-family:Arial,sans-serif;color:#333;background:#f4f4f4;padding:20px;">
      <div style="max-width:600px;margin:0 auto;background:#fff;padding:20px;border-radius:10px;">
        <h1 style="text-align:center;color:#2c3e50;font-size:22px;margin:0 0 5px 0;">Egypt Film Radar</h1>
        {warning_html}
        {section("Performing well in theaters", "#e67e22", theater_cards,
                 f"No new Egyptian releases reached {MIN_RATING} this time.")}
        {section("Now streaming", "#27ae60", streaming_cards,
                 "None of your watchlist movies started streaming this time.")}
        <h3 style="color:#7f8c8d;font-size:14px;margin:28px 0 4px 0;">
          Monitoring for a streaming release ({len(watchlist)})</h3>
        {monitoring_html(watchlist)}
        <p style="text-align:center;color:#b0b8bf;font-size:11px;margin-top:18px;">
          Ratings and cinema listings from ElCinema. Streaming availability from Yango Play,
          ElCinema, and TMDB, with TMDB's streaming data provided by JustWatch.<br>
          This product uses the TMDB API but is not endorsed or certified by TMDB.<br>
          Sent by <a href="{PROJECT_URL}" style="color:#b0b8bf;">Egypt Film Radar</a>.
        </p>
      </div>
    </body></html>"""

    if theater_hits or streaming_hits:
        parts = []
        if theater_hits:
            parts.append(f"{len(theater_hits)} in theaters")
        if streaming_hits:
            parts.append(f"{len(streaming_hits)} now streaming")
        subject = "Egypt Film Radar: " + ", ".join(parts)
    else:
        subject = "Egypt Film Radar: nothing new"
    if warnings:
        subject += " (check needed)"
    if TEST_MODE:
        subject = "[TEST] " + subject
    return subject, body


def build_text(theater_hits, streaming_hits, watchlist):
    """Plain-text version of the email, for mail apps that don't show HTML.
    Spam filters also trust emails more when they include one."""
    def line(m):
        rating = f"{m['rating']}/10 ({m['votes']:,} ratings)" if m.get("votes") else "not rated yet"
        date = f", released {m['release_date']}" if m.get("release_date") else ""
        return f"- {m['title']}: {rating}{date}\n  {m['link']}"
    parts = ["EGYPT FILM RADAR", "",
             "PERFORMING WELL IN THEATERS"]
    parts += [line(m) for m in sorted(theater_hits, key=lambda m: m["rating"], reverse=True)] or ["Nothing new this time."]
    parts += ["", "NOW STREAMING"]
    for m in sorted(streaming_hits, key=lambda m: m["rating"], reverse=True):
        parts.append(line(m))
        parts += [f"  {name}: {p['url']}" for name, p in m["platforms"].items()]
    if not streaming_hits:
        parts.append("Nothing new this time.")
    parts += ["", f"Monitoring {len(watchlist)} movie(s) for a streaming release.", "",
              f"Sent by Egypt Film Radar: {PROJECT_URL}"]
    return "\n".join(parts)


def send_email(subject, body, text):
    msg = MIMEMultipart("alternative")
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = TO_EMAIL
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(EMAIL_ADDRESS or "localhost").split("@")[-1])
    msg.attach(MIMEText(text, "plain", "utf-8"))  # Plain version first, HTML last (the preferred one)
    msg.attach(MIMEText(body, "html", "utf-8"))
    # Port 465 uses SSL from the start; other ports (usually 587) upgrade with STARTTLS.
    if SMTP_PORT == 465:
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=60)
    else:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=60)
    with server:
        if SMTP_PORT != 465:
            server.starttls()
        server.login(SMTP_USER, EMAIL_PASSWORD)
        server.send_message(msg)


# ---------------------------------------------------------------------------
def main():
    print(f"Starting Egypt Film Radar {VERSION} ({today()})"
          + (" [TEST MODE]" if TEST_MODE else "") + (" [DRY RUN]" if DRY_RUN else ""))

    if not DRY_RUN and not (EMAIL_ADDRESS and EMAIL_PASSWORD and TO_EMAIL):
        print("ERROR: EMAIL_ADDRESS, EMAIL_PASSWORD or TO_EMAIL is missing. Check the .env file.")
        sys.exit(1)

    print(f"Settings: rating >= {MIN_RATING} with >= {MIN_VOTES} votes, re-check {RECHECK_WEEKS} weeks, "
          f"watch for streaming {STREAMING_WATCH_MONTHS} months, every {RUN_EVERY_DAYS} days, "
          f"Arabic titles {'on' if SHOW_ARABIC_TITLES else 'off'}, TMDB key {'set' if TMDB_API_KEY else 'MISSING'}")

    state = load_state()

    # The task runs every week; this makes the newsletter go out every RUN_EVERY_DAYS.
    # (-1 day of slack so a run that lands a few hours early still counts.)
    # Test and dry runs always go ahead, since they don't save anything.
    if not (TEST_MODE or DRY_RUN) and state.get("last_run"):
        waited = days_since(state["last_run"])
        if waited < RUN_EVERY_DAYS - 1:
            print(f"Last newsletter went out {waited} day(s) ago. Next one is due in "
                  f"{RUN_EVERY_DAYS - waited} day(s), so nothing to do this week.")
            return
    yango_index = load_yango_index()
    tmdb = TMDB(TMDB_API_KEY)
    theater_hits, streaming_hits, warnings = run_checks(state, yango_index, tmdb)
    subject, body = build_email(theater_hits, streaming_hits, warnings, state["watchlist"])
    text = build_text(theater_hits, streaming_hits, state["watchlist"])
    size_kb = len(body.encode("utf-8")) / 1024
    if size_kb > 95:
        print(f"Note: the email is {size_kb:.0f} KB. Gmail hides the end of emails over ~100 KB behind "
              "a 'View entire message' link.")

    if DRY_RUN:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(PREVIEW_FILE, "w", encoding="utf-8") as f:
            f.write(body)
        print(f"DRY RUN: email not sent. Subject would be: {subject}")
        print(f"Preview saved to {PREVIEW_FILE}. Nothing was saved to state.json.")
        return

    try:
        send_email(subject, body, text)
        print(f"Email sent: {subject}")
    except Exception as e:
        # Don't save, so nothing is marked as reported and next run tries again.
        print(f"ERROR: email failed to send: {hide_secrets(e)}")
        sys.exit(1)

    if TEST_MODE:
        print("TEST MODE: nothing was saved to state.json.")
        return

    state["last_run"] = today()
    save_state(state)
    print("Saved to state.json. Done.")


if __name__ == "__main__":
    main()

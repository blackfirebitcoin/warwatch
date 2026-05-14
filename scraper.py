"""WARWATCH async scrapers.

Each scraper is a coroutine that returns list[Event]. Scrapers are
deliberately lightweight: parse and discard HTML, never store it in
memory longer than necessary.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import feedparser
import httpx
from bs4 import BeautifulSoup

from models import Event, get_conn, log_scrape, upsert_event

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
CONFIG = json.loads((ROOT / "config.json").read_text())

# Classification uses word-boundary regex to avoid substring collisions
# (prior bug: "raid" matched "aid" → HUMANITARIAN). Stems end with `\w*` so
# e.g. "mobiliz" catches mobilize/mobilized/mobilization.
CLASH_WORDS = ("clash\\w*", "ambush\\w*", "firefight\\w*", "infantry", "engagement", "ground combat", "gun battle",
               "mutual attack\\w*", "exchang\\w* of fire", "exchang\\w* fire",
               "trad\\w* fire", "trad\\w* blows", "cross-?border fire")
AIRSTRIKE_WORDS = ("airstrike\\w*", "air ?strike\\w*", "air raid", "raid\\w*",
                   "strike on", "strikes on", "strike hit", "strike targeted",
                   "struck", "hit target\\w*", "hit military", "hit a military",
                   "hit in (?:\\w+ )?strike\\w*", "hit by (?:\\w+ )?strike\\w*",
                   "idf strike\\w*", "israeli strike\\w*", "israel targets",
                   "israel targeted", "us strike\\w*", "drone strike\\w*", "missile strike\\w*",
                   "precision strike\\w*", "munition\\w*",
                   "bomb\\w*", "bombard\\w*",
                   "targeted site\\w*", "targeted a",
                   "warplane\\w*", "shell\\w*", "artiller\\w*", "tank fire",
                   "levelled", "leveled", "demolish\\w*",
                   # actor + kinetic verb — catches "Israeli army strikes",
                   # "IDF warplanes hit", "Iranian forces struck"
                   "(?:army|military|forces?|troops?|jets?|warplanes?|aircraft|planes?|idf|israel|israeli|iran|iranian|us|american|russia\\w*|syria\\w*) (?:strikes?|struck)",
                   # "launches/conducts/carried out (fresh) strikes/raids/attack"
                   "(?:launch\\w*|conduct\\w*|carry out|carried out|carrying out|execut\\w*|stag\\w*) (?:(?:fresh|new|major|massive|heavy|precision|air|ground|night|pre-?dawn|overnight) )?(?:strike\\w*|raid\\w*|attack\\w*|bombing|airstrike\\w*)",
                   # attack/strike hits a target/facility/position
                   "attack\\w* hit", "strikes? hit\\w*",
                   "hit (?:\\w+ ){0,3}facilit\\w*", "hit (?:\\w+ ){0,3}positions?",
                   "hit (?:\\w+ ){0,3}sites?", "hit (?:\\w+ ){0,3}base\\w*",
                   # destruction of X
                   "destruction of", "destroyed (?:a |an |the )?\\w+")
ROCKET_WORDS = ("rocket\\w*", "missile\\w*", "projectile\\w*", "salvo\\w*", "barrage\\w*",
                "uav\\w*", "drone\\w*", "intercept\\w*", "iron dome")
GROUND_WORDS = ("invasion", "invading", "ground operation\\w*", "ground op\\w*", "incursion\\w*",
                "division", "brigade entered", "encircle\\w*", "encirclement", "advanced into",
                "ground forces", "push into", "battle for")
CASUALTY_WORDS = ("kill\\w*", "martyr\\w*", "dead", "wounded", "casualt\\w*", "fatalit\\w*",
                  "toll", "bodies recovered", "massacre\\w*", "mourn\\w*",
                  "eliminat\\w*", "neutraliz\\w*", "assassin\\w*", "slain", "slew")
CEASEFIRE_WORDS = ("ceasefire\\w*", "cease-fire", "truce\\w*", "pause in fighting")
DIPLO_WORDS = ("meeting\\w*", "talks", "envoy\\w*", "negotiat\\w*", "statement", "summit\\w*",
               "diplomat\\w*", "resolution", "accord\\w*",
               "pressur\\w*", "postpon\\w*", "stated", "says?",
               "spokesperson", "spokesman", "spokeswoman",
               "deal", "within reach", "response", "warn\\w*", "condemn\\w*",
               "holding off", "threat\\w*", "urged", "called on",
               "demand\\w*", "propos\\w* peace", "backchannel",
               # speech-act verbs — catches cleric/official statements like
               # "Sheikh Qassem: enemy resorted to bloody crimes" that would
               # otherwise fall through to the DEPLOYMENT fallback.
               "resort\\w* to", "vow\\w* to", "pledg\\w* to",
               "declar\\w* that", "accus\\w*")
HUMANIT_WORDS = ("aid convoy", "aid delivery", "humanitarian aid", "convoy\\w*", "refugee\\w*",
                 "displaced", "evacuat\\w*", "hospital\\w*", "humanitarian")
DEPLOY_WORDS = ("deployed", "deployment\\w*", "reinforce\\w*", "mobiliz\\w*", "reserves called",
                "called up", "leaflet\\w*")
# Supply-side commodity events: physical disruption of hydrocarbon flow.
# Tight verb+object compositions so that mere mentions of "tanker" or
# "pipeline" in an analysis piece don't trip this — must co-occur with a
# disruptive verb or explicit incident noun.
SUPPLY_DISRUPT_WORDS = (
    "tanker\\w* (?:hit|struck|attacked|seized|boarded|damaged|aflame|ablaze|on fire|divert\\w*|steer\\w* clear|halt\\w*|avoid\\w*|detain\\w*)",
    "(?:hit|struck|attacked|seized|boarded|detain\\w*) (?:\\w+ ){0,3}(?:tanker\\w*|vlcc|vessel\\w*|ship\\w*|cargo ship|container vessel)",
    "pipeline (?:\\w+ ){0,5}(?:after attack\\w*|after strike\\w*|following attack\\w*)",
    "restor\\w* (?:\\w+ ){0,3}(?:capacity|operation\\w*|flow\\w*) (?:\\w+ ){0,3}pipeline",
    "vessel (?:attacked|struck|hit|boarded|seized|aflame|reports being hit)",
    "board (?:a |an |the )?vessel",
    "(?:armed\\w*|rebel\\w*|pirate\\w*) (?:attempt\\w*|board\\w*|seiz\\w*)",
    "pipeline (?:explosion|sabotage|attack\\w*|fire|rupture|shutdown|outage|sever\\w*|bomb\\w*|damaged|disrupt\\w*|back to (?:full )?capacity)",
    "refinery (?:fire|attack\\w*|shutdown|outage|damaged|struck|hit|blockade\\w*)",
    "(?:oil|gas|lng|fuel) (?:terminal|refinery|depot|storage) (?:attacked|struck|closed|evacuated|shut|shutdown|halted|blockade\\w*)",
    "oilfield (?:attacked|struck|seized|damaged|shut)",
    "(?:platform|rig) (?:attacked|struck|evacuated)",
    "(?:halt\\w*|suspend\\w*|disrupt\\w*|shut\\w*|cut\\w*|slash\\w*) (?:\\w+ ){0,3}(?:output|production|flow\\w*|exports?|shipments?|transit|crude|oil|gas|lng)",
    "supply disruption\\w*", "lng disruption\\w*", "energy disruption\\w*",
    "(?:missile|drone|rocket) (?:hit|struck|targeted|attack\\w*) (?:\\w+ ){0,3}(?:tanker|pipeline|refinery|terminal|oilfield|platform|vessel|ship)",
    "blockade of (?:\\w+ ){0,2}(?:hormuz|strait|red sea|bab|suez|refinery|terminal|oil|port|shipping)",
    "(?:us|uk|israeli|iranian|yemeni|houthi) blockade",
    "ships? (?:divert\\w*|halt\\w* transit|avoid\\w*|steer clear)",
    "shipping (?:giant\\w* )?halt\\w*",
)
# Demand/price-setting policy events. Governments and cartels, not incidents.
MARKET_MOVE_WORDS = (
    "output cut\\w*", "production cut\\w*", "output (?:increase\\w*|hike\\w*|rais\\w*)",
    "production (?:increase\\w*|hike\\w*|rais\\w*)",
    "quota (?:cut\\w*|rais\\w*|chang\\w*|adjust\\w*|set)",
    # verb-first phrasing: "raises production quota", "cuts output",
    # "hikes output target" — verb leads, commodity noun follows.
    "(?:rais\\w*|hik\\w*|cut\\w*|slash\\w*|reduc\\w*|increas\\w*|lift\\w*|boost\\w*) (?:\\w+ ){0,2}(?:production|output|quota|target)",
    "barrel\\w* per day cut\\w*",
    "(?:lift\\w*|impos\\w*|tighten\\w*|ease\\w*|expand\\w*) (?:\\w+ ){0,3}sanctions?",
    "oil embargo", "embargo on (?:\\w+ )?(?:oil|gas|crude)",
    "spr release\\w*", "release from (?:the )?(?:strategic petroleum reserve|spr)",
    "draw\\w* from (?:the )?(?:strategic petroleum reserve|spr)",
    "tap\\w* (?:the )?(?:strategic petroleum reserve|spr)",
    "opec\\+? (?:agree\\w*|decid\\w*|announc\\w*|extend\\w*|roll\\w* over|receive\\w* (?:\\w+ ){0,3}compensation plan)",
    "price cap", "g7 cap",
    # price / tax / duty moves (stems on the verb + \w* on the noun).
    "(?:rais\\w*|lift\\w*|cut\\w*|slash\\w*|reduc\\w*|hik\\w*|increas\\w*) (?:\\w+ ){0,3}(?:price\\w*|tariff\\w*|duty|duties|export dut\\w*|tax\\w*)",
    "(?:suspend\\w*|impos\\w*|restor\\w*) (?:\\w+ ){0,3}(?:tax\\w*|duty|duties|tariff\\w*)",
    # compensation / production plans submitted to OPEC
    "compensation plan\\w*",
    # price records / fresh highs — policy-adjacent market milestones
    "(?:oil|crude|brent|wti) (?:hits?|surge\\w*|jump\\w*|spike\\w*) (?:\\w+ ){0,3}(?:record|high|new high|\\$\\d+)",
    "fresh record (?:high )?(?:near |above )?\\$\\d+",
)

# Compile once as OR'd word-boundary alternations.
import re as _re


def _compile(words: tuple) -> _re.Pattern:
    return _re.compile(r"\b(?:" + "|".join(words) + r")\b", _re.IGNORECASE)


_PAT = {
    "CLASH": _compile(CLASH_WORDS),
    "GROUND_OP": _compile(GROUND_WORDS),
    "AIRSTRIKE": _compile(AIRSTRIKE_WORDS),
    "ROCKET_FIRE": _compile(ROCKET_WORDS),
    "SUPPLY_DISRUPTION": _compile(SUPPLY_DISRUPT_WORDS),
    "CASUALTY": _compile(CASUALTY_WORDS),
    "MARKET_MOVE": _compile(MARKET_MOVE_WORDS),
    "CEASEFIRE_UPDATE": _compile(CEASEFIRE_WORDS),
    "HUMANITARIAN": _compile(HUMANIT_WORDS),
    "DIPLOMATIC": _compile(DIPLO_WORDS),
    "DEPLOYMENT": _compile(DEPLOY_WORDS),
}

# Priority order when multiple categories match: most operationally significant wins.
# SUPPLY_DISRUPTION sits above AIRSTRIKE / ROCKET_FIRE because the patterns
# require an explicit hydrocarbon target (tanker / pipeline / refinery /
# terminal / oilfield / platform) — when those co-occur with a strike verb
# the supply framing is the operationally significant one ("Houthi missile
# struck oil tanker in Red Sea" → SUPPLY_DISRUPTION, not AIRSTRIKE). The
# target-noun requirement keeps generic kinetic headlines from leaking into
# this bucket. MARKET_MOVE sits below CEASEFIRE_UPDATE but above DIPLOMATIC —
# an OPEC cut is a concrete action with measurable consequences, broader
# than a statement.
CLASSIFY_ORDER = ("CLASH", "GROUND_OP", "SUPPLY_DISRUPTION",
                  "AIRSTRIKE", "ROCKET_FIRE", "CASUALTY",
                  "CEASEFIRE_UPDATE", "MARKET_MOVE",
                  "HUMANITARIAN", "DIPLOMATIC", "DEPLOYMENT")


def classify(text: str) -> str:
    for cat in CLASSIFY_ORDER:
        if _PAT[cat].search(text):
            return cat
    return "DEPLOYMENT"


# Relevance gate. Admitting an item requires both:
#   - at least one "event verb" (concrete kinetic or diplomatic action), and
#   - no blocklist match (economic/analysis/entertainment noise).
# Generic words like "war", "talks", "forces" are NOT sufficient on their
# own — they appear in every op-ed about Middle East politics.
_EVENT_VERB_RE = _re.compile(
    r"\b(strike\w*|struck|hit\b|attacks?\w*|attacked|kill\w*|killed|dead|"
    r"airstrike\w*|air ?strike\w*|airstruck|"
    r"wounded|casualt\w*|martyr\w*|assassin\w*|eliminat\w*|"
    r"rocket\w*|missile\w*|drone\w*|bomb\w*|shell\w*|bombard\w*|artiller\w*|"
    r"intercept\w*|salvo\w*|barrage\w*|"
    r"invasion|invading|incursion\w*|clash\w*|ambush\w*|firefight\w*|"
    r"ceasefire|cease-fire|truce|"
    r"hostage\w*|displaced|evacuat\w*|"
    r"deployed|deployment\w*|mobiliz\w*|reinforce\w*|"
    r"raid\w*|targets?\w*|targeted|detain\w*|"
    r"damag\w*|destroy\w*|destroyed|demolish\w*|level(?:led|ed)|"
    r"incurs?|fired at|launched (?:at|toward|into)|"
    r"convoy\w*|humanit\w*|refugee\w*)",
    _re.IGNORECASE,
)

# Noise splits into two layers: UNIVERSAL fires regardless of gate,
# CONFLICT_ONLY fires only for the conflict relevance gate (commodity
# news legitimately talks about oil prices and macro framings).
_NOISE_UNIVERSAL_RE = _re.compile(
    r"\b(?:"
    # celeb / entertainment
    r"celebrity|gossip|epstein|melania|tesla stock|nfl"
    r"|concert|album|movie|film premiere"
    # pundit / analysis framings
    r"|op-?ed|opinion:|analysis:|explain\w*:|here'?s what|here'?s why"
    r"|podcast|newsletter|weekly roundup|live blog"
    # labor-action collisions with the kinetic "strike" verb. Universal
    # because labor strikes aren't events of interest to either gate.
    r"|port strike\w*|dock strike\w*|workers?'? strike\w*"
    r"|general strike\w*|labou?r strike\w*|hunger strike\w*|strike action"
    # political sidebars unrelated to either conflict or commodity signal
    r"|corruption trial|bribery case|civil suit|court date"
    r")",
    _re.IGNORECASE,
)

_NOISE_CONFLICT_RE = _re.compile(
    r"\b(?:"
    # pure market / commodity framing — not a conflict event. Commodity
    # gate needs these to pass, so they live in the conflict-only layer.
    r"oil price\w*|oil market|oil whiplash|oil end\w*|oil post\w*"
    r"|budget deficit|consumer sentiment|boost\w* sentiment"
    r"|stock\w*|market\w* (?:open|close|deficit|surge)|analyst\w* say"
    r"|outlays?|macroeconom\w*|inflation|gdp|exchange rate|rupee|arbitrage"
    r"|unwinding|commodit\w*|corporate earnings?"
    # pundit framings that mention conflict but aren't events
    r"|could hinder|counting the cost|leaves crisis"
    r"|farmers (?:are )?hit|tourism hit|supply chain hit"
    r")",
    _re.IGNORECASE,
)

# Commodity-side event verbs. Admitting an item requires at least one of
# these plus a pass through _NOISE_UNIVERSAL_RE. Intentionally permissive
# on market/price framing because that's the whole point of this gate.
_COMMODITY_VERB_RE = _re.compile(
    r"\b(?:"
    r"tanker\w*|vessel|vlcc|supertanker|pipeline\w*|refiner\w*|terminal"
    r"|oilfield|oil field|platform|rig"
    r"|barrel\w*|bpd|mbpd|crude|brent|wti|lng|natural gas|gasoline|diesel"
    r"|output|production|quota|embargo|sanction\w*|price cap|spr"
    r"|opec\+?|aramco|adnoc|rosneft|gazprom|lukoil"
    r"|shipment\w*|cargo|export\w*|import\w*"
    r"|chokepoint|strait of hormuz|bab el-mandeb|suez|bosphorus"
    r"|boarded|seized|attack\w*|struck|hit\b|fire|explosion|sabotage"
    r"|shutdown|outage|halt\w*|suspend\w*|disrupt\w*"
    r")",
    _re.IGNORECASE,
)


def is_relevant(text: str) -> bool:
    """Return True if the text looks like a conflict event, not ambient noise.

    Two-gate design: must contain a concrete event verb AND must not match
    any noise pattern (universal or conflict-specific).
    """
    if not text:
        return False
    if _NOISE_UNIVERSAL_RE.search(text) or _NOISE_CONFLICT_RE.search(text):
        return False
    return bool(_EVENT_VERB_RE.search(text))


def is_commodity_relevant(text: str) -> bool:
    """Return True if the text looks like a commodity/energy event.

    Admits market-framing language that `is_relevant` rejects, so a
    "OPEC+ agrees to extend output cuts" headline passes. Still drops
    universal noise (celebrity, op-eds, labor strikes).
    """
    if not text:
        return False
    if _NOISE_UNIVERSAL_RE.search(text):
        return False
    return bool(_COMMODITY_VERB_RE.search(text))


# Google News wraps every title as "Actual headline - Source Name" which
# pollutes summary-similarity dedup. Strip the trailing " - Publisher".
_GNEWS_SUFFIX_RE = _re.compile(r"\s+[-–—]\s+[^-–—]{2,60}$")


def strip_gnews_suffix(title: str) -> str:
    if not title:
        return title
    return _GNEWS_SUFFIX_RE.sub("", title).strip()


def theater_of(text: str, hint: Optional[str] = None) -> str:
    """Pick the theater whose keywords most-densely appear in the text.

    Count-based scoring. The source's theater hint gets a +1 bonus and
    wins any tie — it only loses when another theater has strictly more
    distinct keyword hits. Prevents a Lebanon source mentioning "Iranian
    envoy" from flipping to IRAN just because "iran" is a substring of
    "iranian" (two hits for IRAN).
    """
    t = text.lower()
    scores: dict[str, int] = {}
    for theater, meta in CONFIG["theaters"].items():
        hits = 0
        for kw in meta["keywords"]:
            if kw in t:
                hits += 1
        if hits:
            scores[theater] = hits
    if not scores:
        return hint or "OTHER"
    if hint:
        scores[hint] = scores.get(hint, 0) + 1
    max_score = max(scores.values())
    winners = [t for t, s in scores.items() if s == max_score]
    if len(winners) > 1 and hint in winners:
        return hint
    return sorted(winners)[0]


# Place-name gazetteer built from config keywords. Filters out country/
# region names and organization names — those are useful for *theater*
# tagging but not as dedup keys.
_NON_PLACES = {
    # countries / mega-regions
    "lebanon", "iran", "iranian", "gaza", "gaza strip", "syria", "yemen",
    "yemeni", "israel", "south lebanon", "blue line",
    "middle east", "caspian",
    "golan heights",
    # orgs / actors / adjectives (not places)
    "hezbollah", "hamas", "islamic jihad", "qassam",
    "houthi", "houthis", "ansar allah", "irgc", "pasdaran", "basij",
    "quds force", "unifil",
    # people
    "khamenei", "pezeshkian", "araghchi",
    # too-generic regions
    "bekaa", "beqaa",
    # ENERGY theater non-places (commodity terms that aren't geography
    # and would otherwise pollute the gazetteer with "Oil" / "Opec"
    # "locations"). Keep specific terminals/chokepoints AS places.
    "oil", "crude", "petroleum", "gasoline", "diesel", "jet fuel",
    "brent", "wti", "opec", "opec+", "barrel", "bpd", "mbpd",
    "lng", "natural gas", "liquefied natural gas", "condensate",
    "tanker", "vlcc", "supertanker", "shipment", "cargo",
    "pipeline", "refinery", "refineries", "terminal", "platform",
    "oilfield", "oil field", "oil well", "rig",
    "embargo", "sanctions", "spr", "strategic petroleum reserve",
    "production cut", "output cut", "production quota", "quota",
    "aramco", "saudi aramco", "adnoc", "qatar energy",
    "rosneft", "gazprom", "lukoil", "kuwait petroleum",
    "iea", "eia", "international energy agency",
    "red sea shipping", "gulf of oman",
    "houthi tanker", "tanker attack", "tanker seized", "tanker boarded",
    "pipeline attack", "pipeline sabotage", "pipeline explosion",
}


def _build_gazetteer() -> list[str]:
    seen: set = set()
    out: list[str] = []
    for meta in CONFIG["theaters"].values():
        for kw in meta["keywords"]:
            k = kw.lower().strip()
            if not k or k in _NON_PLACES:
                continue
            if k in seen:
                continue
            seen.add(k)
            out.append(k)
    out.sort(key=len, reverse=True)
    return out


_GAZETTEER = _build_gazetteer()
_GAZETTEER_PATTERNS = [(p, _re.compile(r"\b" + _re.escape(p) + r"\b", _re.IGNORECASE)) for p in _GAZETTEER]


def extract_location(text: str) -> Optional[str]:
    """Scan text for a known place name. Returns the canonical gazetteer form."""
    if not text:
        return None
    for name, pat in _GAZETTEER_PATTERNS:
        if pat.search(text):
            return name.title()
    return None


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_rss_date(entry) -> str:
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc).isoformat()
            except Exception:
                pass
    return now_utc_iso()


# ISO-8601 with optional timezone, matches most <time datetime="..."> attrs
_ISO_RE = _re.compile(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)")


def _try_iso(s: str) -> Optional[str]:
    """Parse common ISO-ish strings to a UTC isoformat. Returns None on failure."""
    if not s:
        return None
    s = s.strip()
    m = _ISO_RE.search(s)
    if not m:
        return None
    raw = m.group(1).replace(" ", "T")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def extract_ts(element, soup=None) -> Optional[str]:
    """Best-effort timestamp extraction from a BeautifulSoup element.

    Tries, in order:
      1. Nested/ancestor <time datetime="...">
      2. itemprop="datePublished"/"dateModified"
      3. data-time / data-timestamp attrs (unix or ISO)
      4. Falls back to None (caller should use first_seen_at)
    """
    if element is None:
        return None

    # 1. <time datetime="..."> anywhere inside this element
    time_el = element.find("time") if hasattr(element, "find") else None
    if time_el is not None:
        dt = time_el.get("datetime") or time_el.get("data-time") or time_el.get_text(" ", strip=True)
        iso = _try_iso(dt or "")
        if iso:
            return iso

    # 2. itemprop date
    for prop in ("datePublished", "dateModified"):
        meta = element.find(attrs={"itemprop": prop}) if hasattr(element, "find") else None
        if meta is not None:
            dt = meta.get("content") or meta.get("datetime") or meta.get_text(" ", strip=True)
            iso = _try_iso(dt or "")
            if iso:
                return iso

    # 3. data attrs on the element itself
    for attr in ("data-time", "data-timestamp", "data-published", "datetime"):
        val = element.get(attr) if hasattr(element, "get") else None
        if not val:
            continue
        if str(val).isdigit():
            try:
                return datetime.fromtimestamp(int(val), tz=timezone.utc).isoformat()
            except Exception:
                pass
        iso = _try_iso(str(val))
        if iso:
            return iso

    return None


async def fetch(client: httpx.AsyncClient, url: str) -> Optional[str]:
    try:
        r = await client.get(url, follow_redirects=True)
        if r.status_code == 200:
            return r.text
    except Exception:
        return None
    return None


# ---------- scrapers ----------

_LUAM_DATE_RE = _re.compile(
    r"/(\d{4})/(\d{1,2})-(january|february|march|april|may|june|july|august|september|october|november|december)-(\d{1,2})",
    _re.IGNORECASE,
)
_MONTHS = {m: i+1 for i, m in enumerate(
    ["january","february","march","april","may","june","july","august","september","october","november","december"]
)}


def _liveuamap_ts_from_link(link: str) -> Optional[str]:
    if not link:
        return None
    m = _LUAM_DATE_RE.search(link)
    if not m:
        return None
    year = int(m.group(1)); day = int(m.group(2)); month = _MONTHS[m.group(3).lower()]; hour = int(m.group(4))
    try:
        return datetime(year, month, day, hour, 0, 0, tzinfo=timezone.utc).isoformat()
    except Exception:
        return None


# LiveUAMap bakes its own metadata into every card's visible text as
# "N hour ago <Location string> <actual event text>". Strip it so the
# summary stored in the DB is just the event text — timestamp and location
# are already captured in their own columns.
_LUAM_TIMEAGO_RE = _re.compile(
    r"^\s*\d+\s+(?:second|minute|hour|day|week|month)s?\s+ago\s+",
    _re.IGNORECASE,
)


def _strip_liveuamap_prefix(text: str, raw_location: Optional[str]) -> str:
    """Strip the time-ago + location prefix LiveUAMap embeds in each card."""
    if not text:
        return text
    cleaned = _LUAM_TIMEAGO_RE.sub("", text, count=1)
    if raw_location:
        loc = raw_location.strip()
        # Match the raw location at the start, with any trailing whitespace/comma.
        if cleaned.lower().startswith(loc.lower()):
            cleaned = cleaned[len(loc):].lstrip(" ,")
    return cleaned.strip()


async def scrape_liveuamap(client: httpx.AsyncClient) -> list[Event]:
    src = CONFIG["sources"]["liveuamap_lebanon"]
    html = await fetch(client, src["url"])
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    events: list[Event] = []
    # LiveUAMap cards structure:
    #   <div class="event" data-x=LON data-y=LAT data-time=unix data-link=...>
    #     <... "N minute ago <Location string> " text ...>
    #     <a title="<actual event text>">...</a>
    #   </div>
    cards = soup.select("div.event")
    if not cards:
        cards = soup.select("#feedler > div")
    for c in cards[:60]:
        a = c.select_one("a[title]")
        if a is None:
            continue
        # The title attribute holds the clean event text — use it directly.
        event_text = (a.get("title") or "").strip()
        if len(event_text) < 15:
            continue

        lat = lon = None
        try:
            lat = float(c.get("data-y")) if c.get("data-y") else None
            lon = float(c.get("data-x")) if c.get("data-x") else None
        except Exception:
            pass

        # Timestamp precedence: data-time (unix) > permalink date > now
        ts_iso = None
        ts_attr = c.get("data-time") or ""
        if ts_attr.isdigit():
            try:
                ts_iso = datetime.fromtimestamp(int(ts_attr), tz=timezone.utc).isoformat()
            except Exception:
                pass
        if not ts_iso:
            ts_iso = _liveuamap_ts_from_link(c.get("data-link") or "")
        if not ts_iso:
            ts_iso = now_utc_iso()

        # Recover the location string: it's the part of the full card text
        # between the "N time ago" prefix and the title text.
        full_text = c.get_text(" ", strip=True)
        after_timeago = _LUAM_TIMEAGO_RE.sub("", full_text, count=1)
        raw_loc = None
        idx = after_timeago.find(event_text)
        if idx > 0:
            raw_loc = after_timeago[:idx].strip(" ,")
        # Canonicalize the raw_loc through the gazetteer; fall back to the
        # canonical form extracted from the event_text, or the raw string.
        loc = extract_location(raw_loc or "") or extract_location(event_text) or raw_loc

        ev = Event(
            timestamp=ts_iso,
            summary=event_text[:400],
            event_type=classify(event_text),
            theater=theater_of(event_text, hint=src["theater_hint"]),
            location=loc,
            lat=lat, lon=lon,
            sources=[{"name": src["name"], "url": src["url"], "attribution": None}],
            confidence="REPORTED",
        )
        events.append(ev)
    return events


def _parse_rss_blob(src: dict, html: str) -> list[Event]:
    """Synchronous RSS body parser — extracted so async callers can
    push it onto a worker thread.

    feedparser.parse + BeautifulSoup.get_text are CPU-bound and were
    running directly on the Textual event loop (and serialized across
    the 14 RSS sources because asyncio coroutines don't actually
    parallelize CPU work). Moving this off-loop lets asyncio.gather
    fan it out across the default thread executor.
    """
    feed = feedparser.parse(html)
    events: list[Event] = []
    for entry in feed.entries[:80]:
        title = strip_gnews_suffix(entry.get("title", ""))
        summary_html = entry.get("summary", "")
        summary = BeautifulSoup(summary_html, "lxml").get_text(" ", strip=True) if summary_html else ""
        summary = strip_gnews_suffix(summary)
        text = f"{title}. {summary}".strip(". ")
        if not text:
            continue
        theater = theater_of(text, hint=src.get("theater_hint"))
        # RSS feed is global — skip items that don't touch a tracked theater
        if theater == "OTHER":
            continue
        # Relevance gate — conflict sources use the kinetic-verb gate,
        # commodity sources use a parallel gate that admits market/policy
        # framing. Pick by source config; default is conflict.
        gate = src.get("relevance_gate", "conflict")
        if gate == "commodity":
            if not is_commodity_relevant(text):
                continue
        else:
            if not is_relevant(text):
                continue
        events.append(Event(
            timestamp=parse_rss_date(entry),
            summary=text[:400],
            event_type=classify(text),
            theater=theater,
            location=extract_location(text),
            sources=[{"name": src["name"], "url": entry.get("link") or src["url"], "attribution": src.get("attribution")}],
            confidence="REPORTED",
        ))
    return events


async def scrape_rss(client: httpx.AsyncClient, key: str) -> list[Event]:
    src = CONFIG["sources"][key]
    html = await fetch(client, src["url"])
    if not html:
        return []
    return await asyncio.to_thread(_parse_rss_blob, src, html)


async def scrape_unifil(client: httpx.AsyncClient) -> list[Event]:
    """Scrape UNIFIL's /en/news listing page.

    The Drupal template wraps each news entry in <article> with a
    `.field--name-title` child and a `<time datetime="...">` tag. The old
    selector (root page + `.views-row`) pulled mission-description
    boilerplate instead of news items — all UNIFIL events in the DB
    ended up being the site's tagline or the mission blurb.

    Also gates items through is_relevant() so peacekeeper-donation and
    minefield-handover pieces are dropped in favor of actual incidents.
    """
    src = CONFIG["sources"]["unifil"]
    html = await fetch(client, src["url"])
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    events: list[Event] = []
    seen_titles: set[str] = set()
    for art in soup.select("article")[:40]:
        title_el = art.select_one(".field--name-title")
        if title_el is None:
            title_el = art.select_one("h1, h2, h3, .title, a")
        if title_el is None:
            continue
        title_text = title_el.get_text(" ", strip=True)
        # Filter placeholder "News" header card and short/dup titles.
        if len(title_text) < 12 or title_text.lower() in ("news", "latest news"):
            continue
        if title_text in seen_titles:
            continue
        seen_titles.add(title_text)
        if not is_relevant(title_text):
            continue
        ts = extract_ts(art) or now_utc_iso()
        events.append(Event(
            timestamp=ts,
            summary=title_text[:400],
            event_type=classify(title_text),
            theater="LEBANON",
            location=extract_location(title_text),
            sources=[{"name": src["name"], "url": src["url"], "attribution": src.get("attribution")}],
            confidence="REPORTED",
        ))
    return events[:15]


SCRAPERS = {
    "liveuamap_lebanon": scrape_liveuamap,
    "lorient_today": lambda c: scrape_rss(c, "lorient_today"),
    "aljazeera_rss": lambda c: scrape_rss(c, "aljazeera_rss"),
    "timesofisrael_liveblog": lambda c: scrape_rss(c, "timesofisrael_liveblog"),
    "reuters_me_rss": lambda c: scrape_rss(c, "reuters_me_rss"),
    "almayadeen": lambda c: scrape_rss(c, "almayadeen"),
    "idf_press": lambda c: scrape_rss(c, "idf_press"),
    "unifil": scrape_unifil,
    # Energy / commodity intelligence lane — all RSS (native or
    # Google-News site-search), route through scrape_rss and gated by
    # is_commodity_relevant via src["relevance_gate"].
    "reuters_energy": lambda c: scrape_rss(c, "reuters_energy"),
    "opec_press": lambda c: scrape_rss(c, "opec_press"),
    "eia_today": lambda c: scrape_rss(c, "eia_today"),
    "ukmto": lambda c: scrape_rss(c, "ukmto"),
    "bloomberg_energy": lambda c: scrape_rss(c, "bloomberg_energy"),
    "oilprice_news": lambda c: scrape_rss(c, "oilprice_news"),
    "lloyds_list": lambda c: scrape_rss(c, "lloyds_list"),
}


async def run_one(client: httpx.AsyncClient, key: str) -> tuple[str, list[Event], str]:
    fn = SCRAPERS[key]
    try:
        events = await fn(client)
        return key, events, "ok"
    except Exception as e:
        return key, [], f"error:{type(e).__name__}:{str(e)[:80]}"


def _is_fresh(ev_ts: str, max_age_days: int) -> bool:
    """Ingest-time freshness gate: drop events older than N days.

    Google News RSS occasionally surfaces months-old articles under a
    site-search query; this keeps the DB focused on contemporary events.
    """
    try:
        s = ev_ts[:-1] + "+00:00" if ev_ts.endswith("Z") else ev_ts
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return True  # if we can't parse it, don't drop it
    age = datetime.now(timezone.utc) - dt
    return age.total_seconds() <= max_age_days * 86400


def _process_results(results: list, global_max_age: int) -> dict:
    """Synchronous DB processing after HTTP fetches complete.

    Extracted so run_all() can offload this to a thread via asyncio.to_thread,
    keeping the Textual event loop free during dedup and upsert work.
    """
    summary = {"total_new": 0, "total_merged": 0, "total_stale": 0, "per_source": {}}
    conn = get_conn()
    try:
        for key, events, status in results:
            src_cfg = CONFIG["sources"][key]
            src_name = src_cfg["name"]
            src_max_age = int(src_cfg.get("max_age_days", global_max_age))
            new_n = merged_n = stale_n = 0
            for ev in events:
                if not _is_fresh(ev.timestamp, src_max_age):
                    stale_n += 1
                    continue
                try:
                    r = upsert_event(conn, ev)
                except Exception:
                    continue
                if r == "new":
                    new_n += 1
                elif r == "merged":
                    merged_n += 1
            log_scrape(conn, src_name, "ok" if status == "ok" else status, new_n + merged_n)
            summary["total_new"] += new_n
            summary["total_merged"] += merged_n
            summary["total_stale"] += stale_n
            summary["per_source"][src_name] = {
                "status": status,
                "found": len(events),
                "new": new_n,
                "merged": merged_n,
                "stale": stale_n,
            }
        conn.commit()
    finally:
        conn.close()

    try:
        from alerts import fire_pending_alerts
        summary["alerts_fired"] = fire_pending_alerts()
    except Exception:
        summary["alerts_fired"] = 0
    return summary


async def run_all() -> dict:
    """Full scrape cycle. Returns summary dict."""
    headers = {"User-Agent": CONFIG["user_agent"], "Accept": "*/*"}
    global_max_age = int(CONFIG.get("ingest_max_age_days", 3))
    # Split connect/read timeouts: a slow DNS or TCP handshake shouldn't
    # eat the same 20s budget as a legitimately slow body. read=20s
    # matches the previous global timeout so behavior on slow servers
    # is unchanged; connect=5s prevents a dead host from holding back
    # the rest of the asyncio.gather.
    request_timeout = CONFIG["request_timeout"]
    timeout = httpx.Timeout(request_timeout, connect=5.0)
    limits = httpx.Limits(max_connections=30, max_keepalive_connections=15)
    async with httpx.AsyncClient(
        headers=headers, timeout=timeout, limits=limits, http2=False,
    ) as client:
        results = await asyncio.gather(*(run_one(client, k) for k in SCRAPERS.keys()))

    # DB dedup + upsert is synchronous/blocking; run in a thread so the
    # Textual event loop stays responsive while ~300 find_dup calls execute.
    return await asyncio.to_thread(_process_results, results, global_max_age)


if __name__ == "__main__":
    print(asyncio.run(run_all()))

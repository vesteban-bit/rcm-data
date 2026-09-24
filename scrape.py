#!/usr/bin/env python3
"""
scrape.py  ·  Rental Community Miami / Miami Rental Concierge  ·  v2

Reads the list of communities from the Google Sheet (or communities.yml when
no sheet is configured), reads each community's public page once, and writes
the results to two places:

    1. back into the Google Sheet (prices, floor plans, facts, status)
    2. docs/data.json in this repo (served by GitHub Pages, and git history
       becomes a free daily price log)

A non-technical editor adds a community by pasting its URL into a new row of
the sheet. The next run fills in everything the page publishes. Nothing in
this file needs to change to add a property.

WHAT IT READS FROM A RENTCAFE PAGE (verified on Miro Brickell, 2026-09-21)
    JSON-LD AggregateOffer      price range
    JSON-LD FAQPage             units available, per-bedroom minimums
    div.fp-item data-* attrs    every floor plan: name, beds, baths, sqft,
                                price range, has units, price drop
    page text                   year built, total units, stories, Walk Score,
                                Transit Score, pet policy, application fee
    <title>                     street address

HOUSE RULES ENFORCED HERE
    · One request per community per day. Set RCM_HTML_CACHE to a folder while
      developing and pages are read from disk instead of re-fetched.
    · Facts only. Never marketing copy, never photos.
    · Last good value is kept, with its original date, when a read fails.
    · Access mode is explicit per community: "honesto" (named bot) by default,
      "navegador" (Chrome TLS fingerprint) only when a human chose it in the
      sheet. The mode used is recorded with every result.

Environment
    SHEET_API_URL   Apps Script web app URL (…/exec). Empty = use communities.yml
    SHEET_TOKEN     shared secret, same value as SCRAPER_TOKEN in the sheet
    RCM_HTML_CACHE  optional folder for day-scoped HTML cache (development)
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import yaml
from bs4 import BeautifulSoup
from curl_cffi import requests

ROOT = Path(__file__).parent
OUTPUT = ROOT / "docs" / "data.json"
CONFIG = ROOT / "communities.yml"

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

SHEET_API_URL = os.environ.get("SHEET_API_URL", "").strip()
SHEET_TOKEN = os.environ.get("SHEET_TOKEN", "").strip()
HTML_CACHE = os.environ.get("RCM_HTML_CACHE", "").strip()

HONEST_UA = (
    "RentalCommunityMiamiBot/1.0 "
    "(+https://rentalcommunitymiami.com/bot; contact@rentalcommunitymiami.com)"
)
IMPERSONATE_PROFILE = "chrome124"

RENT_FLOOR, RENT_CEILING = 800, 25_000
MAX_DRIFT = 0.40
TIMEOUT = 30
DELAY_RANGE = (3, 8)
FAILURE_THRESHOLD = 1 / 3

_robots_cache: dict[str, RobotFileParser | None] = {}


class ReadError(Exception):
    """A failure with a message written for the person reading the sheet."""

    def __init__(self, code: str, human: str):
        super().__init__(f"{code}: {human}")
        self.code = code
        self.human = human


# ───────────────────────────────────────────────────────── transport ──

def _get(url: str, identity: str, headers: dict | None = None):
    h = {"Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
         "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        h.update(headers)
    if identity == "impersonate":
        return requests.get(url, impersonate=IMPERSONATE_PROFILE, headers=h, timeout=TIMEOUT)
    h["User-Agent"] = HONEST_UA
    return requests.get(url, headers=h, timeout=TIMEOUT)


def robots_allows(url: str) -> bool:
    """
    Fetched with the impersonated client on purpose: RobotFileParser.read()
    goes through urllib, gets a 403 from the WAF, and then treats the whole
    site as disallowed. Verified failure mode.
    """
    host = urlparse(url).netloc
    if host not in _robots_cache:
        rp = RobotFileParser()
        try:
            r = _get(f"{urlparse(url).scheme}://{host}/robots.txt", "impersonate")
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
                _robots_cache[host] = rp
            else:
                _robots_cache[host] = None
        except Exception:
            _robots_cache[host] = None
    rp = _robots_cache[host]
    return True if rp is None else rp.can_fetch("*", url)


def fetch_html(url: str, identity: str, slug: str) -> str:
    """One request per community per day, cache-aware for development."""
    cache_file = None
    if HTML_CACHE:
        cache_file = Path(HTML_CACHE) / TODAY / f"{slug}.html"
        if cache_file.exists():
            return cache_file.read_text(encoding="utf-8")

    try:
        r = _get(url, identity)
    except Exception as exc:
        raise ReadError("red", f"La página no respondió ({type(exc).__name__}). Se reintenta en la próxima corrida.")

    if r.status_code == 403 and identity == "honest":
        raise ReadError(
            "bloqueado_honesto",
            "El sitio bloquea el modo honesto. Si quieres incluir esta comunidad, "
            "cambia 'modo_acceso' a 'navegador' en la hoja.",
        )
    if r.status_code == 404:
        raise ReadError("404", "La página ya no existe (404). Revisa la URL.")
    if r.status_code != 200:
        raise ReadError(f"http_{r.status_code}", f"El sitio respondió con error {r.status_code}.")

    if cache_file:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(r.text, encoding="utf-8")
    return r.text


# ───────────────────────────────────────────────────────────── parsing ──

def _to_int(value) -> int | None:
    digits = re.sub(r"[^\d]", "", str(value or ""))
    return int(digits) if digits else None


def _price_range(text: str) -> tuple[int | None, int | None]:
    nums = [_to_int(n) for n in re.findall(r"\$\s?([\d,]{3,9})", text or "")]
    nums = [n for n in nums if n]
    if not nums:
        return None, None
    return min(nums), max(nums)


def _beds(text: str) -> int | None:
    t = (text or "").strip().lower()
    if t.startswith("studio"):
        return 0
    m = re.match(r"(\d+)", t)
    return int(m.group(1)) if m else None


def _baths(text: str) -> float | None:
    m = re.match(r"\s*(\d+(?:\.\d+)?)", text or "")
    return float(m.group(1)) if m else None


def _jsonld_blocks(soup: BeautifulSoup) -> list[dict]:
    """RentCafe's FAQPage block has raw newlines inside strings; repair it."""
    blocks: list[dict] = []
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        for candidate in (raw, re.sub(r"[\n\r\t]+", " ", raw)):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            blocks.extend(parsed if isinstance(parsed, list) else [parsed])
            break
    return blocks


def parse_floorplans(soup: BeautifulSoup) -> list[dict]:
    """
    Every floor plan RentCafe lists, from the data-* attributes on div.fp-item.

    Attributes are the most stable part of the markup: they exist so the
    site's own JavaScript can filter plans, so they survive visual redesigns
    that would break any CSS selector. Plans are deduplicated by id because
    the same card can appear under several filter tabs.

    Note on units: only the first few units per plan are in the HTML (the
    rest load on "Show more units"), so we record whether a plan HAS units
    and the earliest date shown, not a count. The building-level count comes
    from the FAQ block instead.
    """
    plans, seen = [], set()
    for item in soup.select("div.fp-item"):
        fid = item.get("data-floorplan-id") or item.get("data-id") or item.get("data-name")
        if not fid or fid in seen:
            continue
        seen.add(fid)

        lo, hi = _price_range(item.get("data-rent", ""))
        # "632 - 704 Sqft" is a range. Stripping non-digits from the whole
        # string once produced a 632,704 sq ft studio.
        sizes = [_to_int(n) for n in re.findall(r"[\d,]+", item.get("data-size") or "")]
        sizes = [n for n in sizes if n and 150 <= n <= 10000]
        dates = []
        for unit in item.select("tr.fp-unit"):
            cells = [c.get_text(" ", strip=True) for c in unit.find_all("td")]
            for c in cells:
                if re.fullmatch(r"(?i)now|available now|\d{1,2}/\d{1,2}(/\d{2,4})?|[A-Z][a-z]{2}\s+\d{1,2}", c):
                    dates.append(c)

        plans.append({
            "nombre": (item.get("data-name") or "").strip(),
            "habitaciones": _beds(item.get("data-beds", "")),
            "banos": _baths(item.get("data-baths", "")),
            "sqft": min(sizes) if sizes else None,
            "sqft_max": max(sizes) if len(sizes) > 1 else None,
            "desde": lo,
            "hasta": hi,
            "disponible": (item.get("data-hasunits") or "").lower() == "true",
            "disponible_desde": ("Now" if any(d.lower().endswith("now") for d in dates) else (dates[0] if dates else None)),
            "bajo_precio": (item.get("data-has-price-drop") or item.get("data-haspricedrop") or "").lower() == "true",
        })

    # Plans with a price first, cheapest first, then the "ask for pricing" ones.
    plans.sort(key=lambda p: (p["desde"] is None, p["desde"] or 0, p["habitaciones"] or 0))
    return plans


def parse_facts(soup: BeautifulSoup, text: str) -> dict:
    """Building facts published on the page. Only filled into the sheet when
    the editor has left the cell empty: a human value always wins."""
    facts: dict = {}

    m = re.search(r"([\d,]+)\s+apartments?\s+in\s+the\s+community", text, re.I)
    if m:
        facts["unidades"] = _to_int(m.group(1))

    m = re.search(r"(\d{1,3})(?:\s*-\s*(\d{1,3}))?\s+stor(?:y|ies)", text, re.I)
    if m:
        facts["pisos"] = int(m.group(2) or m.group(1))

    m = re.search(r"(?:constructed|built)\s+in\s+((?:19|20)\d{2})", text, re.I)
    if m:
        facts["anio"] = int(m.group(1))

    m = re.search(r"Walk\s*Score\W{0,3}\s*:?\s*(\d{1,3})", text, re.I)
    if m:
        facts["walk_score"] = int(m.group(1))

    m = re.search(r"Transit\s*Score\W{0,3}\s*:?\s*(\d{1,3})", text, re.I)
    if m:
        facts["transit_score"] = int(m.group(1))

    m = re.search(r"Application\s+fees?\s*\$\s?([\d,]+)", text, re.I)
    if m:
        facts["app_fee"] = _to_int(m.group(1))

    m = re.search(r"Security\s+deposit\s*\$\s?([\d,]+)", text, re.I)
    if m:
        facts["deposito"] = _to_int(m.group(1))

    facts.update(parse_pets(text))

    title = soup.title.get_text(strip=True) if soup.title else ""
    m = re.match(r"^.+?\bApartments?,\s*(.+?)\s+-\s+RentCafe$", title)
    if m:
        facts["direccion"] = m.group(1).strip()

    return facts


BREEDS = re.compile(r"rottweiler|pit\s*bull|doberman|akita|chow|mastiff|wolf|staffordshire|breed\s+restrict", re.I)


def parse_pets(text: str) -> dict:
    """
    Pet policy, read only from the "Pet policy" section so a line elsewhere
    ("dog park", "pet spa") cannot be mistaken for permission. Wording varies
    a lot between buildings; verified variants include "Pet policy - allowed
    Cats Dogs", "Pets allowed.", "Max weight 50 lb each", "Pet limit 2
    allowed", "One-time pet fees $450", "Monthly pet rent $30", and breed
    lists such as "Rottweiler, doberman pinscher, pit bull".
    """
    i = text.find("Pet policy")
    if i < 0:
        return {}
    sec = text[i:i + 600]
    out: dict = {}

    if re.search(r"\bno\s+pets\b|pets?\s+(?:are\s+)?not\s+allowed", sec, re.I):
        out["mascotas"] = "none"
        return out

    allowed = re.search(r"pet policy\s*-\s*allowed|pets?\s+allowed|pet[- ]friendly|we\s+welcome", sec, re.I)
    cats = re.search(r"\bcats?\b", sec, re.I)
    dogs = re.search(r"\bdogs?\b", sec, re.I)
    if cats and dogs:
        out["mascotas"] = "cats_dogs"
    elif cats and not dogs:
        out["mascotas"] = "cats_only"
    elif allowed or re.search(r"pet (?:rent|fee)", sec, re.I):
        out["mascotas"] = "pets_allowed"
    if out.get("mascotas") in ("cats_dogs", "pets_allowed") and BREEDS.search(sec):
        out["mascotas"] = "cats_dogs_restricted"

    m = re.search(r"max(?:imum)?\s+weight\s*(\d{1,3})\s*(?:lb|lbs|pounds)", sec, re.I)
    if m:
        out["mascotas_peso"] = int(m.group(1))
    m = re.search(r"pet\s+limit\s*(\d)|maximum\s+of\s+(two|three|\d)", sec, re.I)
    if m:
        out["mascotas_max"] = int({"two": "2", "three": "3"}.get((m.group(2) or "").lower(), m.group(1) or m.group(2)))
    m = re.search(r"one-time\s+pet\s+fees?\s*\$\s?([\d,]+)", sec, re.I)
    if m:
        out["mascotas_fee"] = _to_int(m.group(1))
    m = re.search(r"monthly\s+pet\s+rent\s*\$\s?([\d,]+)", sec, re.I)
    if m:
        out["mascotas_renta"] = _to_int(m.group(1))
    return out


CONCESSION_TERMS = re.compile(
    r"(?:up\s+to\s+)?\d+(?:\.\d+)?\s*(?:month|week)s?'?\s*(?:of\s+)?free"
    r"|free\s+month\b"
    r"|waived\s+\w*\s*fee"
    r"|look\s*(?:and|&|\+)\s*lease"
    r"|(?:reduced|no|zero)\s+deposit"
    r"|\$\s?[\d,]{3,}\s*off\b",
    re.I,
)


def parse_concessions(text: str) -> str | None:
    """
    Two facts, never conflated: is a special advertised, and what are its
    terms. Only explicit quantities count as terms ("1 month free"); the bare
    word "free" matched a loyalty-programme blurb once and is not trusted.
    """
    m = CONCESSION_TERMS.search(text)
    if m:
        start = max(0, m.start() - 90)
        lead = text[start:m.start()]
        cut = max(lead.rfind(". "), lead.rfind("! "), lead.rfind("? "), lead.rfind("* "))
        if cut != -1:
            start += cut + 2
        return text[start: m.end() + 80].strip()
    if re.search(r"\bRent specials?\b", text, re.I):
        return "ADVERTISED_UNVERIFIED"
    return None


def parse_page(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    out: dict = {"planos": [], "hechos": {}}

    faq_mins: dict[int, int] = {}
    for block in _jsonld_blocks(soup):
        about = block.get("about") or {}
        if isinstance(about, dict):
            offers = about.get("offers") or {}
            if isinstance(offers, dict) and offers.get("@type") == "AggregateOffer":
                out["precio_min"] = _to_int(offers.get("lowPrice"))
                out["precio_max"] = _to_int(offers.get("highPrice"))

        if block.get("@type") == "FAQPage":
            for entry in block.get("mainEntity") or []:
                q = str(entry.get("name", ""))
                a = str((entry.get("acceptedAnswer") or {}).get("text", ""))
                if re.search(r"how many units", q, re.I):
                    m = re.search(r"\b(\d{1,4})\b", a)
                    if m:
                        out["disponibilidad"] = int(m.group(1))
                if re.search(r"what floorplans", q, re.I):
                    for beds, price in re.findall(r"(\d+)-bedrooms?\s+apartments?\s+from\s+\$([\d,]+)", a, re.I):
                        faq_mins[int(beds)] = _to_int(price)

    plans = parse_floorplans(soup)
    if not plans and faq_mins:
        plans = [{"nombre": None, "habitaciones": b, "banos": None, "sqft": None,
                  "desde": p, "hasta": None, "disponible": True,
                  "disponible_desde": None, "bajo_precio": False}
                 for b, p in sorted(faq_mins.items())]
    out["planos"] = plans

    # No AggregateOffer? Derive the range from the floor plans.
    priced = [p for p in plans if p["desde"]]
    if not out.get("precio_min") and priced:
        out["precio_min"] = min(p["desde"] for p in priced)
        out["precio_max"] = max((p["hasta"] or p["desde"]) for p in priced)

    text = unescape(soup.get_text(" ", strip=True))
    text = re.sub(r"\s+", " ", text)

    if "disponibilidad" not in out:
        m = re.search(r"\b(\d{1,4})\s+units?\s+available", text, re.I)
        if m:
            out["disponibilidad"] = int(m.group(1))

    out["concesiones"] = parse_concessions(text)
    out["hechos"] = parse_facts(soup, text)
    return out


# ────────────────────────────────────────────────────────── strategies ──

def strategy_auto(t: dict) -> dict:
    html = fetch_html(t["url"], t["identity"], t["slug"])
    data = parse_page(html)
    if not data.get("precio_min"):
        raise ReadError(
            "sin_precios",
            "Leímos la página pero no encontramos precios. Revisa que la URL sea "
            "la página principal de la comunidad, no la de un plano o unidad.",
        )
    return data


def strategy_xhr(t: dict) -> dict:
    """Internal JSON endpoint captured with DevTools. Fallback only."""
    r = _get(t["url_endpoint"], t["identity"], headers=t.get("headers"))
    if r.status_code != 200:
        raise ReadError(f"http_{r.status_code}", f"El endpoint respondió con error {r.status_code}.")
    node = r.json()
    for key in (t.get("json_path") or "").split("."):
        if key:
            node = node[key]
    f = t.get("fields") or {}
    plans = []
    for fp in node:
        price = _to_int(fp.get(f.get("price", "minRent")))
        if price:
            plans.append({"nombre": fp.get(f.get("name", "name")),
                          "habitaciones": _to_int(fp.get(f.get("beds", "beds"))) or 0,
                          "banos": None, "sqft": None, "desde": price, "hasta": None,
                          "disponible": True, "disponible_desde": None, "bajo_precio": False})
    if not plans:
        raise ReadError("sin_precios", "El endpoint no devolvió precios.")
    return {"precio_min": min(p["desde"] for p in plans),
            "precio_max": max(p["desde"] for p in plans),
            "planos": plans, "hechos": {}}


def strategy_manual(t: dict) -> dict:
    m = t.get("manual") or {}
    if not m.get("precio_min"):
        raise ReadError("manual_vacio", "Entrada manual sin precio.")
    return {"precio_min": _to_int(m["precio_min"]), "precio_max": _to_int(m.get("precio_max")),
            "planos": m.get("planos") or [], "concesiones": m.get("concesiones"),
            "hechos": {}, "_manual_date": m.get("verificado")}


STRATEGIES = {"auto": strategy_auto, "jsonld": strategy_auto, "xhr": strategy_xhr, "manual": strategy_manual}


# ───────────────────────────────────────────────────────────── guards ──

def guard(data: dict, previous_min: int | None) -> list[str]:
    notes: list[str] = []
    lo, hi = data.get("precio_min"), data.get("precio_max")

    if not lo or lo <= 0:
        raise ReadError("sin_precios", "No se obtuvo un precio válido.")
    if not (RENT_FLOOR <= lo <= RENT_CEILING):
        raise ReadError("fuera_de_rango",
                        f"Precio ${lo:,} fuera del rango esperado (${RENT_FLOOR:,} a ${RENT_CEILING:,}). "
                        "Probablemente la página cambió; se mantiene el precio anterior.")

    if hi:
        if hi < lo:
            data["precio_min"], data["precio_max"] = hi, lo
            notes.append("min y max invertidos, corregido")
        elif hi > RENT_CEILING:
            data["precio_max"] = None
            notes.append(f"máximo ${hi:,} descartado por excesivo")
        elif hi > lo * 4:
            # Usually a furnished or penthouse listing. Use the floor plans'
            # own ceiling when we have one, it is far more representative.
            fp_hi = max((p["hasta"] or p["desde"] or 0) for p in data.get("planos") or [{"hasta": 0, "desde": 0}])
            if fp_hi and fp_hi < hi:
                data["precio_max"] = fp_hi
                notes.append(f"máximo publicado ${hi:,} parece amueblado o penthouse; se usa ${fp_hi:,} de los planos")
            else:
                notes.append(f"rango muy amplio ${lo:,} a ${hi:,}, revisar")

    # Individual plans outside the band are dropped, not the whole read.
    for p in data.get("planos") or []:
        if p.get("desde") and not (RENT_FLOOR <= p["desde"] <= RENT_CEILING):
            p["desde"] = p["hasta"] = None

    if previous_min:
        drift = abs(lo - previous_min) / previous_min
        if drift > MAX_DRIFT:
            raise ReadError("salto_precio",
                            f"El precio cambió {drift:.0%} de golpe (de ${previous_min:,} a ${lo:,}). "
                            "Se mantiene el anterior hasta confirmarlo a mano.")
        if drift > 0.12:
            notes.append(f"cambio de {drift:.0%} desde ${previous_min:,}, conviene confirmar")

    return notes


# ─────────────────────────────────────────────────────────── targets ──

def _slugify(name: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def load_targets() -> tuple[list[dict], str]:
    """From the Google Sheet when configured, otherwise communities.yml."""
    if SHEET_API_URL and SHEET_TOKEN:
        r = requests.get(SHEET_API_URL, params={"action": "targets", "token": SHEET_TOKEN},
                         timeout=60, allow_redirects=True)
        if r.status_code != 200:
            raise SystemExit(f"Sheet targets request failed: HTTP {r.status_code}")
        payload = r.json()
        if not payload.get("ok"):
            raise SystemExit(f"Sheet refused targets request: {payload.get('error')}")
        targets = []
        for t in payload.get("targets", []):
            if not t.get("url"):
                continue
            targets.append({
                "slug": t["slug"],
                "nombre": t.get("nombre") or t["slug"],
                "barrio": t.get("barrio") or "",
                "url": t["url"],
                "estrategia": "auto",
                "identity": "impersonate" if (t.get("modo") or "").strip().lower() == "navegador" else "honest",
                "activo": bool(t.get("activo")),
                "previous_min": _to_int(t.get("precio_min_prev")),
            })
        return targets, "sheet"

    rows = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or []
    targets = []
    for c in rows:
        targets.append({
            **c,
            "slug": c.get("slug") or _slugify(c["nombre"]),
            "estrategia": c.get("estrategia", "auto"),
            "identity": c.get("identity", "honest"),
            "activo": c.get("activo", True),
            "previous_min": None,
        })
    return targets, "yaml"


def post_results(results: list[dict]) -> None:
    if not (SHEET_API_URL and SHEET_TOKEN):
        return
    body = json.dumps({"token": SHEET_TOKEN, "action": "results", "fecha": TODAY, "results": results},
                      ensure_ascii=False)
    r = requests.post(SHEET_API_URL, data=body.encode("utf-8"),
                      headers={"Content-Type": "application/json"}, timeout=120, allow_redirects=True)
    try:
        reply = r.json()
    except Exception:
        reply = {"ok": False, "error": f"HTTP {r.status_code}, non-JSON reply"}
    if not reply.get("ok"):
        print(f"\nWARN  sheet write-back failed: {reply.get('error')}")
    else:
        print(f"\nsheet updated: {reply.get('updated', '?')} rows, {reply.get('plans', '?')} floor plans")


# ────────────────────────────────────────────────────────────── main ──

def main() -> int:
    targets, source = load_targets()
    print(f"{len(targets)} communities from {source} · {TODAY}\n")

    if not targets:
        # Most likely the SHEET_API_URL / SHEET_TOKEN secrets are missing and
        # it fell back to the (empty) communities.yml. Writing now would wipe
        # data.json, so stop and let GitHub send the failure email instead.
        print("No communities to read. Check the SHEET_API_URL and SHEET_TOKEN "
              "secrets in GitHub. Nothing was written.")
        return 1

    previous_all: dict = {}
    if OUTPUT.exists():
        try:
            previous_all = json.loads(OUTPUT.read_text(encoding="utf-8")).get("comunidades", {})
        except json.JSONDecodeError:
            print("WARN  previous data.json unreadable, starting clean")

    current: dict = {}
    results: list[dict] = []
    ok_count, failures = 0, []

    for t in targets:
        slug = t["slug"]
        previous = previous_all.get(slug)
        baseline = t.get("previous_min") or (previous or {}).get("precio_min")
        strategy = t.get("estrategia", "auto")

        try:
            target_url = t.get("url") or t.get("url_endpoint") or ""
            if strategy != "manual" and target_url and not robots_allows(target_url):
                raise ReadError("robots", "El sitio no permite leer esta página (robots.txt). Descártala.")

            data = STRATEGIES[strategy](t)
            notes = guard(data, baseline)
            verified = data.pop("_manual_date", None) or TODAY

            record = {
                "nombre": t["nombre"], "barrio": t["barrio"], "activo": t.get("activo", True),
                "precio_min": data["precio_min"], "precio_max": data.get("precio_max"),
                "disponibilidad": data.get("disponibilidad"), "concesiones": data.get("concesiones"),
                "planos": data.get("planos") or [], "hechos": data.get("hechos") or {},
                "verificado": verified, "fallos_seguidos": 0,
                "estrategia": strategy, "identidad": t["identity"], "notas": notes,
            }
            current[slug] = record
            results.append({"slug": slug, "ok": True, **record})
            ok_count += 1

            plans_priced = sum(1 for p in record["planos"] if p.get("desde"))
            print(f"OK   {slug:<26} ${record['precio_min']:,}-${record['precio_max'] or 0:,}  "
                  f"{len(record['planos'])} plans ({plans_priced} priced)  "
                  f"facts: {','.join(sorted(record['hechos'])) or '-'}"
                  + (f"\n     notes: {'; '.join(notes)}" if notes else ""))

        except Exception as exc:
            human = exc.human if isinstance(exc, ReadError) else f"Error inesperado: {type(exc).__name__}: {exc}"
            code = exc.code if isinstance(exc, ReadError) else "error"
            failures.append(slug)
            print(f"FAIL {slug:<26} {code}: {human}")

            results.append({"slug": slug, "ok": False, "codigo": code, "error": human,
                            "identidad": t["identity"]})
            if previous:
                previous["fallos_seguidos"] = previous.get("fallos_seguidos", 0) + 1
                previous["ultimo_error"] = human[:300]
                current[slug] = previous
                print(f"     kept last good value from {previous.get('verificado')} "
                      f"(streak {previous['fallos_seguidos']})")

        if strategy != "manual" and not (HTML_CACHE and (Path(HTML_CACHE) / TODAY / f"{slug}.html").exists()):
            time.sleep(random.uniform(*DELAY_RANGE))

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps({"generado": TODAY, "comunidades": current}, ensure_ascii=False, indent=2),
                      encoding="utf-8")

    post_results(results)

    stale = [s for s, c in current.items() if c.get("fallos_seguidos", 0) >= 3]
    print(f"\n{ok_count} ok · {len(failures)} failed · {len(stale)} on a 3+ streak")
    if failures:
        print("failed: " + ", ".join(failures))

    active = [t for t in targets if t.get("activo", True)]
    active_failed = [s for s in failures if any(t["slug"] == s and t.get("activo", True) for t in targets)]
    if active and len(active_failed) > len(active) * FAILURE_THRESHOLD:
        print("\nToo many active communities failed; exiting non-zero so GitHub sends the email.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

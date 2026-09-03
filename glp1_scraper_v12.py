#!/usr/bin/env python3
"""
glp1_scraper_v12.py

v12: pen needles dropped - some pharmacies include them with the pen, so a tracked
     price would misrepresent the real cost. Ozempic/Rybelsus stay out: confirmed not
     marketed in NZ (searches return nothing at every retailer).
v10: collect-only additions - Ozempic, Rybelsus (oral semaglutide) and pen needles.
     These aren't shown on the site yet; the point is to have history when they matter.
     Retailers may not list them at all (Ozempic is reportedly not marketed in NZ), so
     search-based sources simply return nothing rather than erroring.
v9: Contrave/Duromine kept (the page lists them separately from the GLP-1 tables) but
    every product now gets a real strength label, so no row renders blank.
v8: Saxenda titles parsed into a pack label.
v7: normaliser only rewrites bare dose labels, so Zoom pack descriptions are left intact.
v6: strengths normalised in one place (1mg -> 1.0mg, "12.5 mg" -> 12.5mg) so the same
    dose from different retailers lands on the same row.
v5: Bargain Chemist switched to one search/suggest.json call per drug (all strengths,
    prices and availability in a single request) instead of per-product .js calls.
v4: all 11 CW products (Mounjaro + Wegovy); stock flags left blank where the retailer
    does not reliably expose them (CW boilerplate, Shopify backorder-as-available).
v3: CW anchored on "NZD NON-SUBSIDISED PRICE" (exact); --find-cw helper to discover product IDs.
v2: smarter Chemist Warehouse price detection + --debug-cw; Bargain Chemist uses .js (has stock); Zoom columns tidied.

Pulls public GLP-1 prices from NZ retailers into a single dated CSV.
Sources:
  - Chemist Warehouse  (HTML product pages, JSON-LD first, regex fallback)
  - Bargain Chemist    (Shopify /products/<handle>.json)
  - PillDrop           (HTML price tables)
  - Zoom Pharmacy      (WooCommerce Store API)

Usage:
  pip install requests beautifulsoup4
  python glp1_scraper_v12.py            # appends to data/prices.csv
  python glp1_scraper_v12.py --dry-run  # prints, doesn't write

Each source is isolated: if one breaks, the others still record.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-NZ,en;q=0.9"}
TIMEOUT = 25
PAUSE = 2.0  # seconds between requests to the same site - be polite

OUT_PATH = os.path.join("data", "prices.csv")
FIELDS = ["date", "retailer", "drug", "strength", "price_nzd", "available", "source_url", "note"]


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get(url):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r



def norm_strength(s):
    """Normalise dose labels so retailers agree: '1mg'/'1.0 MG' -> '1.0mg'.

    Sub-milligram doses keep two decimals (0.25mg); whole numbers get one
    (5mg stays 5mg, but 1mg becomes 1.0mg to match how CW and PillDrop write it).
    Anything without a mg figure is passed through unchanged (Zoom pack titles).
    """
    if not s:
        return s
    s = s.strip()
    # Only rewrite labels that ARE a dose ("1mg", "12.5 mg"). Anything longer is a
    # pack description (Zoom lists "... 8mg/90mg Tablets, 112 pack") - leave it alone.
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*mg", s, re.I)
    if not m:
        return s
    v = float(m.group(1))
    if v < 1:
        return f"{v:g}mg"          # 0.25mg, 0.5mg
    if v == int(v) and v in (1,):  # only 1 needs the .0 to match the others
        return f"{v:.1f}mg"
    return f"{v:g}mg"              # 2.5mg, 5mg, 12.5mg, 15mg


def row(retailer, drug, strength, price, available, url, note=""):
    return {
        "date": now_iso(),
        "retailer": retailer,
        "drug": drug,
        "strength": norm_strength(strength),
        "price_nzd": f"{price:.2f}" if price is not None else "",
        "available": "" if available is None else str(bool(available)).lower(),
        "source_url": url,
        "note": note,
    }


# ---------------------------------------------------------------------------
# Chemist Warehouse
# ---------------------------------------------------------------------------
CW_PRODUCTS = [
    ("Mounjaro", "2.5mg",  "https://www.chemistwarehouse.co.nz/buy/165180/mounjaro-2-5mg-kwikpen"),
    ("Mounjaro", "5mg",    "https://www.chemistwarehouse.co.nz/buy/165181/mounjaro-5mg-kwikpen"),
    ("Mounjaro", "7.5mg",  "https://www.chemistwarehouse.co.nz/buy/165182/mounjaro-7-5mg-kwikpen"),
    ("Mounjaro", "10mg",   "https://www.chemistwarehouse.co.nz/buy/165183/mounjaro-10mg-kwikpen"),
    ("Mounjaro", "12.5mg", "https://www.chemistwarehouse.co.nz/buy/165184/mounjaro-12-5mg-kwikpen"),
    ("Mounjaro", "15mg",   "https://www.chemistwarehouse.co.nz/buy/165217/mounjaro-15mg-kwikpen"),
    ("Wegovy",   "0.25mg", "https://www.chemistwarehouse.co.nz/buy/156879/wegovy-0-25mg-flexpen"),
    ("Wegovy",   "0.5mg",  "https://www.chemistwarehouse.co.nz/buy/156880/wegovy-0-5mg-flexpen"),
    ("Wegovy",   "1.0mg",  "https://www.chemistwarehouse.co.nz/buy/156881/wegovy-1mg-flexpen"),
    ("Wegovy",   "1.7mg",  "https://www.chemistwarehouse.co.nz/buy/156882/wegovy-1-7mg-flexpen"),
    ("Wegovy",   "2.4mg",  "https://www.chemistwarehouse.co.nz/buy/156883/wegovy-2-4mg-flexpen"),
]


MIN_PLAUSIBLE = 100.0   # GLP-1 pens are never under $100 - filters "free shipping over $50" etc.

DEBUG_CW = False


def _plausible(v):
    try:
        v = float(str(v).replace(",", "").replace("$", "").strip())
    except Exception:
        return None
    return v if v >= MIN_PLAUSIBLE else None


def _cw_price(soup, html):
    """Try progressively looser strategies. Returns (price, note)."""
    # 0) Chemist Warehouse prints the real per-strength price immediately before
    #    "NZD NON-SUBSIDISED PRICE". Anchor on that - other $ figures on the page
    #    are shipping thresholds, sample minimums, or the visitor's own cart total.
    text0 = soup.get_text(" ", strip=True)
    m0 = re.search(r"\$\s?([\d,]+\.\d{2})\s*NZD\s*NON-?SUBSIDIS", text0, re.I)
    if m0:
        v = _plausible(m0.group(1))
        if v:
            return v, "non-subsidised anchor"
    # 1) JSON-LD
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        for item in (data if isinstance(data, list) else [data]):
            offers = item.get("offers") if isinstance(item, dict) else None
            if not offers:
                continue
            for o in (offers if isinstance(offers, list) else [offers]):
                v = _plausible(o.get("price"))
                if v:
                    return v, "json-ld"
    # 2) microdata / meta
    for el in soup.select('[itemprop="price"], meta[property="product:price:amount"], meta[property="og:price:amount"]'):
        v = _plausible(el.get("content") or el.get_text())
        if v:
            return v, "itemprop/meta"
    # 3) data-price attributes
    for el in soup.select("[data-price], [data-product-price]"):
        v = _plausible(el.get("data-price") or el.get("data-product-price"))
        if v:
            return v, "data-price"
    # 4) any element whose class/id mentions 'price'
    for el in soup.find_all(True, attrs={"class": re.compile("price", re.I)}):
        m = re.search(r"\$\s?([\d,]+(?:\.\d{2})?)", el.get_text(" ", strip=True))
        if m:
            v = _plausible(m.group(1))
            if v:
                return v, f"class~price ({' '.join(el.get('class', []))[:40]})"
    for el in soup.find_all(True, id=re.compile("price", re.I)):
        m = re.search(r"\$\s?([\d,]+(?:\.\d{2})?)", el.get_text(" ", strip=True))
        if m:
            v = _plausible(m.group(1))
            if v:
                return v, f"id~price ({el.get('id')})"
    # 5) inline JSON blobs like "price":459.99
    for m in re.finditer(r'"(?:price|Price|salePrice|currentPrice)"\s*:\s*"?([\d]+(?:\.\d{2})?)', html):
        v = _plausible(m.group(1))
        if v:
            return v, "inline-json"
    # 6) last resort: first plausible $ amount in visible text
    text = soup.get_text(" ", strip=True)
    for m in re.finditer(r"\$\s?([\d,]+(?:\.\d{2})?)", text):
        v = _plausible(m.group(1))
        if v:
            return v, "regex-fallback - verify"
    return None, "no price found"


def scrape_chemist_warehouse():
    out = []
    for drug, strength, url in CW_PRODUCTS:
        try:
            html = get(url).text
            soup = BeautifulSoup(html, "html.parser")
            price, note = _cw_price(soup, html)
            if DEBUG_CW:
                print(f"\n--- DEBUG {url}")
                text = soup.get_text(" ", strip=True)
                for m in re.finditer(r"\$\s?[\d,]+(?:\.\d{2})?", text):
                    a, b = max(0, m.start() - 60), min(len(text), m.end() + 40)
                    print(f"  {m.group(0):>10}  ...{text[a:b]}...")
                for m in re.finditer(r'"[A-Za-z]*[Pp]rice[A-Za-z]*"\s*:\s*"?[\d.]+', html):
                    print(f"  inline: {m.group(0)}")
            # Chemist Warehouse does not expose a reliable per-product stock flag -
            # the words "out of stock"/"backorder" appear in page boilerplate, so
            # guessing produced false negatives. Leave blank rather than mislead.
            available = None
            out.append(row("Chemist Warehouse", drug, strength, price, available, url, note))
        except Exception as e:
            out.append(row("Chemist Warehouse", drug, strength, None, None, url, f"ERROR {e}"))
        time.sleep(PAUSE)
    return out


# ---------------------------------------------------------------------------
# Bargain Chemist (Shopify)
# ---------------------------------------------------------------------------
# Bargain Chemist: one call per drug returns every strength with price + availability.
BC_DRUGS = ["wegovy", "mounjaro", "saxenda"]

# Map a product title to a tidy strength label, e.g.
#   "WEGOVY 2.4 MG FLEXTOUCH 9.6mg/3ml" -> "2.4mg"
#   "Mounjaro 12.5mg Kwikpen"           -> "12.5mg"
def _bc_strength(title):
    m = re.search(r"(\d+(?:\.\d+)?)\s*mg\b", title, re.I)
    return f"{m.group(1)}mg" if m else title.strip()


def scrape_bargain_chemist():
    out = []
    for drug in BC_DRUGS:
        url = ("https://www.bargainchemist.co.nz/search/suggest.json"
               f"?q={drug}&resources[type]=product&resources[limit]=20")
        try:
            products = get(url).json()["resources"]["results"]["products"]
            for p in products:
                title = p.get("title", "")
                if drug not in title.lower():
                    continue  # search can return loosely related items
                price = float(str(p.get("price", "0")).replace(",", ""))
                if price <= 0:
                    continue
                purl = "https://www.bargainchemist.co.nz" + p.get("url", "")
                # Shopify's `available` is true even when backorders are allowed,
                # so treat only an explicit false as a real out-of-stock signal.
                available = False if p.get("available") is False else None
                out.append(row("Bargain Chemist", drug.title(),
                               _bc_strength(title), price, available, purl))
        except Exception as e:
            out.append(row("Bargain Chemist", drug.title(), "", None, None, url, f"ERROR {e}"))
        # note: a search that simply finds nothing records no row, which is correct -
        # it means the retailer doesn't list it, not that the scrape failed.
        time.sleep(PAUSE)
    return out


# ---------------------------------------------------------------------------
# PillDrop (SilverStripe, plain HTML tables)
# ---------------------------------------------------------------------------
PILLDROP_PAGES = [
    ("Wegovy",   "https://pilldrop.co.nz/services/wegovy/"),
    ("Mounjaro", "https://pilldrop.co.nz/services/mounjaro/"),
]


def scrape_pilldrop():
    out = []
    for drug, url in PILLDROP_PAGES:
        try:
            soup = BeautifulSoup(get(url).text, "html.parser")
            seen = set()
            for tr in soup.find_all("tr"):
                cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
                if len(cells) < 2:
                    continue
                strength, price_txt = cells[0], cells[1]
                m = re.search(r"\$\s?([\d,]+(?:\.\d{2})?)", price_txt)
                if not m or not re.search(r"\d", strength):
                    continue
                if strength in seen:   # tables appear twice on the page
                    continue
                seen.add(strength)
                out.append(row("PillDrop", drug, strength, float(m.group(1).replace(",", "")), None, url))
            if not seen:
                out.append(row("PillDrop", drug, "", None, None, url, "ERROR no table rows found"))
        except Exception as e:
            out.append(row("PillDrop", drug, "", None, None, url, f"ERROR {e}"))
        time.sleep(PAUSE)
    return out


# ---------------------------------------------------------------------------
# Zoom Pharmacy (WooCommerce Store API)
# ---------------------------------------------------------------------------
ZOOM_SEARCHES = ["saxenda", "contrave", "duromine", "xenical"]


def _zoom_label(title):
    """Turn a Zoom product title into a short strength/pack label.

    "Saxenda (Liraglutide) 2 x 15ml Injection (6 x 5ml injections)" -> "2 x 15ml"
    "Duromine 15mg Capsules, 30 pack"                                -> "15mg, 30 pack"
    "Contrave ... 8mg/90mg Tablets, 112 pack"                        -> "8mg/90mg, 112 pack"
    Never returns an empty string - a blank label renders as an empty table row.
    """
    # Strip the bracketed "(3 x 5ml injections)" detail first - the pack size we
    # want is the one before it ("2 x 15ml Injection").
    head = re.sub(r"\(.*?\)", " ", title)
    m = re.search(r"(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*ml", head, re.I)
    if m:
        return f"{m.group(1)} x {m.group(2)}ml"
    m = re.search(r"(\d+(?:\.\d+)?)\s*ml", head, re.I)
    if m:
        return f"1 x {m.group(1)}ml"
    # Tablets/capsules: keep the dose and the pack count.
    dose = re.search(r"(\d+(?:\.\d+)?(?:mg|mcg)(?:\s*/\s*\d+(?:\.\d+)?(?:mg|mcg))?)", head, re.I)
    pack = re.search(r"(\d+)\s*pack", head, re.I)
    if dose and pack:
        return f"{dose.group(1).replace(' ', '')}, {pack.group(1)} pack"
    if dose:
        return dose.group(1).replace(" ", "")
    cleaned = re.sub(r"\(.*?\)", "", title)
    for brand in ("saxenda", "contrave", "duromine", "xenical"):
        cleaned = re.sub(brand, "", cleaned, flags=re.I)
    return cleaned.strip(" -,") or title.strip()


def scrape_zoom():
    out = []
    for term in ZOOM_SEARCHES:
        url = f"https://zoompharmacy.co.nz/wp-json/wc/store/v1/products?search={term}&per_page=20"
        try:
            items = get(url).json()
            for it in items:
                name = it.get("name", "")
                if term not in name.lower():
                    continue
                prices = it.get("prices", {})
                minor = int(prices.get("minor_unit", 2))
                price = float(prices.get("price", "0")) / (10 ** minor)
                if price <= 0:
                    continue
                out.append(row("Zoom Pharmacy", term.title(), _zoom_label(name),
                               price, it.get("is_in_stock"), it.get("permalink", url)))
        except Exception as e:
            out.append(row("Zoom Pharmacy", term.title(), "", None, None, url, f"ERROR {e}"))
        time.sleep(PAUSE)
    return out


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--debug-cw", action="store_true", help="print every $ amount found on Chemist Warehouse pages")
    args = ap.parse_args()
    global DEBUG_CW
    DEBUG_CW = args.debug_cw

    rows = []
    for fn in (scrape_chemist_warehouse, scrape_bargain_chemist, scrape_pilldrop, scrape_zoom):
        try:
            rows.extend(fn())
        except Exception as e:  # belt and braces - a source must never kill the run
            print(f"[{fn.__name__}] fatal: {e}", file=sys.stderr)

    for r in rows:
        print(f"{r['retailer']:<18} {r['drug']:<10} {r['strength']:<40} {r['price_nzd']:>9}  {r['available']:<6} {r['note']}")

    if args.dry_run:
        return

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    new_file = not os.path.exists(OUT_PATH)
    with open(OUT_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        w.writerows(rows)
    print(f"\nwrote {len(rows)} rows -> {OUT_PATH}")


if __name__ == "__main__":
    main()

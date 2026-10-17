"""Check US retailer prices, append to data/history.csv, write site/prices.json.

Runs three times a day (9:00, 13:00, 17:00 America/New_York). Set FORCE=1 to run
outside those hours. Writes alerts.md when a watch hits a strictly lower price
than anything logged before.
"""
import csv, json, os, re, sys, urllib.error, urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
HIST = ROOT / "data" / "history.csv"
FIELDS = ["ts", "id", "price", "regular", "retailer", "url", "source", "note"]
RUN_HOURS = {9, 13, 17}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (403, 429, 503):  # bot protection: retry in a real browser
            return get_browser(url)
        raise


def get_browser(url):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(user_agent=UA, locale="en-US")
        resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
        if resp is None or resp.status >= 400:
            raise OSError(f"browser got HTTP {resp.status if resp else 'no response'}")
        if "json" in resp.headers.get("content-type", ""):
            body = resp.text()
        else:
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            body = page.content()
        browser.close()
        return body


def num(x):
    try:
        v = float(str(x).replace("$", "").replace(",", ""))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def shopify_pick(products):
    """Cheapest in-stock variant across products as (price, regular, in_stock); falls back to out of stock."""
    live, sold_out = [], []
    for p in products:
        for v in p.get("variants", []):
            price, cmp_ = num(v.get("price")), num(v.get("compare_at_price"))
            if price is None:
                continue
            (sold_out if v.get("available") is False else live).append((price, cmp_ or price))
    if live:
        return (*min(live), True)
    return (*min(sold_out), False) if sold_out else None


def from_shopify_product(src):
    url = src["url"].split("?")[0].rstrip("/")
    p = json.loads(get(url + ".js"))  # .js prices are in cents
    for v in p.get("variants", []):
        for k in ("price", "compare_at_price"):
            if v.get(k):
                v[k] = num(v[k]) / 100
    res = shopify_pick([p])
    return (*res[:2], url, res[2]) if res else None


def from_shopify_collection(src):
    data = json.loads(get(src["url"].rstrip("/") + "/products.json?limit=250"))
    pat = re.compile(src["match"], re.I)
    res = shopify_pick([p for p in data.get("products", []) if pat.search(p.get("title", "") + " " + p.get("handle", ""))])
    return (*res[:2], src["url"], res[2]) if res else None


def from_shopify_search(src):
    """Shopify storefront search; returns the cheapest match as (price, regular, url, in_stock)."""
    from urllib.parse import quote
    url = (f"{src['base']}/search/suggest.json?q={quote(src['query'])}"
           "&resources%5Btype%5D=product&resources%5Blimit%5D=10")
    prods = json.loads(get(url))["resources"]["results"]["products"]
    pat = re.compile(src["match"], re.I)
    hits = [p for p in prods if pat.search(p.get("title", "") + " " + p.get("handle", ""))]
    if not hits:
        print(f"  search '{src['query']}' on {src['base']}: {len(prods)} results, none matched; "
              f"titles: {[p.get('title') for p in prods[:5]]}", file=sys.stderr)
    best = None
    for p in hits:
        price = num(p.get("price"))
        key = (p.get("available") is False, price or 1e9)
        if price and (best is None or key < best[0]):
            best = (key, (price, num(p.get("compare_at_price_max")) or price,
                          src["base"] + p["url"].split("?")[0], not key[0]))
    return best[1] if best else None


def from_page(src):
    html = get(src["url"])
    prices = []
    for blob in re.findall(r'<script[^>]+ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            walk(json.loads(blob), prices)
        except ValueError:
            pass
    if not prices:
        for pat in (r'property=["\'](?:product|og):price:amount["\'][^>]*content=["\']([\d.,]+)',
                    r'content=["\']([\d.,]+)["\'][^>]*property=["\'](?:product|og):price:amount',
                    r'itemprop=["\']price["\'][^>]*content=["\']([\d.,]+)'):
            prices += [num(m) for m in re.findall(pat, html, re.I)]
    prices = [p for p in prices if p]
    return (min(prices), max(prices), src["url"], True) if prices else None


def walk(node, acc):
    if isinstance(node, dict):
        for k in ("price", "lowPrice"):
            if k in node and node.get("@type") in ("Offer", "AggregateOffer", None):
                v = num(node[k])
                if v:
                    acc.append(v)
        for v in node.values():
            walk(v, acc)
    elif isinstance(node, list):
        for v in node:
            walk(v, acc)


PARSERS = {"shopify_product": from_shopify_product,
           "shopify_collection": from_shopify_collection, "shopify_search": from_shopify_search,
           "page": from_page}


def fetch(w):
    """Check every source; return (offers, errors). Each offer is (price, regular, retailer, url, in_stock)."""
    offers, errors = [], []
    for src in w["sources"]:
        where = src.get("url", src.get("base"))
        before = len(errors)
        try:
            res = PARSERS[src["type"]](src)
            if res:
                offers.append((res[0], res[1], src.get("retailer", w["retailer"]), res[2], res[3]))
            else:
                errors.append(f"no price at {where}")
        except Exception as e:  # network error, 403, bad JSON
            errors.append(f"{where}: {type(e).__name__} {e}")
        if len(errors) > before:
            print(f"  {w['id']}: {errors[-1]}", file=sys.stderr)
    return offers, "; ".join(errors)[:300]


def main():
    now = datetime.now(timezone.utc)
    if os.environ.get("FORCE") != "1" and now.astimezone(ZoneInfo("America/New_York")).hour not in RUN_HOURS:
        print("Outside the 9/13/17 ET run hours; skipping.")
        Path(os.environ.get("GITHUB_OUTPUT", os.devnull)).open("a").write("ran=false\n")
        return
    watches = json.loads((ROOT / "watches.json").read_text())
    rows = list(csv.DictReader(HIST.open()))
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    alerts, summary = [], []
    for w in watches:
        prior = [float(r["price"]) for r in rows if r["id"] == w["id"] and r["price"]]
        has_live = any(r["source"] == "live" for r in rows if r["id"] == w["id"])
        offers, err = fetch(w)
        for price, regular, retailer, url, in_stock in offers:
            rows.append(dict(ts=ts, id=w["id"], price=f"{price:.2f}", regular=f"{regular:.2f}",
                             retailer=retailer, url=url, source="live", note="" if in_stock else "out of stock"))
        if offers:
            price, _, retailer, url, in_stock = min(offers, key=lambda o: (not o[4], o[0]))
            if in_stock and has_live and price < min(prior):  # first live run only replaces unverified seeds
                alerts.append(f"- **{w['watch']}**: ${price:.2f} at {retailer} (previous low ${min(prior):.2f}). [Buy]({url})")
        else:
            print(f"FAILED {w['id']}: {err}", file=sys.stderr)
        summary.append((w, offers, err))
    with HIST.open("w", newline="") as f:
        wr = csv.DictWriter(f, FIELDS)
        wr.writeheader()
        wr.writerows(rows)
    out = []
    for w, offers, err in summary:
        hist = [r for r in rows if r["id"] == w["id"] and r["price"]]
        if offers:
            price, regular, retailer, url, in_stock = min(offers, key=lambda o: (not o[4], o[0]))
            earlier = [float(r["price"]) for r in hist if r["ts"] != ts]
            checked = ts
        else:  # every source failed: show the cheapest price from the latest earlier check
            latest = [r for r in hist if r["ts"] == hist[-1]["ts"]]
            low = min(latest, key=lambda r: float(r["price"]))
            price, regular, retailer, url = float(low["price"]), float(low["regular"] or low["price"]), low["retailer"], low["url"]
            in_stock = low["note"] != "out of stock"
            earlier = [float(r["price"]) for r in hist if r["ts"] != hist[-1]["ts"]]
            checked = hist[-1]["ts"]
        past_low = min(earlier) if earlier else price
        status = "new_low" if price < past_low else "at_low" if price == past_low else "above"
        by_ts = {}
        for r in hist:
            by_ts[r["ts"]] = min(by_ts.get(r["ts"], 1e9), float(r["price"]))
        out.append(dict(id=w["id"], watch=w["watch"], ref=w["ref"], retailer=retailer,
                        today=price, past_low=min(past_low, price), regular=regular,
                        off_regular=round(100 * (regular - price) / regular, 1) if regular else 0,
                        status=status, buy_url=url, checked=checked, stale=not offers, in_stock=in_stock,
                        note="" if offers else err,
                        offers=[dict(retailer=r["retailer"], price=float(r["price"]), url=r["url"], in_stock=r["note"] != "out of stock")
                                for r in sorted((r for r in hist if r["ts"] == ts), key=lambda r: float(r["price"]))],
                        history=[[t, p] for t, p in by_ts.items()]))
    (ROOT / "site").mkdir(exist_ok=True)
    (ROOT / "site" / "prices.json").write_text(json.dumps({"updated": ts, "watches": out}, indent=1))
    alert_file = ROOT / "alerts.md"
    alert_file.unlink(missing_ok=True)
    if alerts:
        alert_file.write_text("New lowest price logged:\n\n" + "\n".join(alerts) + "\n")
    Path(os.environ.get("GITHUB_OUTPUT", os.devnull)).open("a").write("ran=true\n")
    print(f"Checked {len(summary)} watches, {sum(1 for s in summary if not s[1])} fully failed, {len(alerts)} alerts.")


if __name__ == "__main__":
    main()

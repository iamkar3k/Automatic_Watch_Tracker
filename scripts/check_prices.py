"""Check US retailer prices, append to data/history.csv, write site/prices.json.

Runs three times a day (9:00, 13:00, 17:00 America/New_York). Set FORCE=1 to run
outside those hours. Writes alerts.md when a watch hits a strictly lower price
than anything logged before.
"""
import csv, json, os, re, sys, urllib.request
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
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def num(x):
    try:
        v = float(str(x).replace("$", "").replace(",", ""))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def shopify_variants(p):
    """Shopify JSON lists prices in dollars (products.json) or cents (.js)."""
    out = []
    for v in p.get("variants", []):
        price, cmp_ = num(v.get("price")), num(v.get("compare_at_price"))
        if price is None or (v.get("available") is False):
            continue
        out.append((price, cmp_ or price))
    return out


def from_shopify_product(src):
    url = src["url"].split("?")[0].rstrip("/")
    p = json.loads(get(url + ".js"))  # .js prices are in cents
    out = [(price / 100, cmp_ / 100) for price, cmp_ in shopify_variants(p)]
    return min(out) if out else None


def from_shopify_collection(src):
    data = json.loads(get(src["url"].rstrip("/") + "/products.json?limit=250"))
    pat = re.compile(src["match"], re.I)
    out = []
    for p in data.get("products", []):
        if pat.search(p.get("title", "") + " " + p.get("handle", "")):
            out += shopify_variants(p)
    return min(out) if out else None


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
    return (min(prices), max(prices)) if prices else None


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
           "shopify_collection": from_shopify_collection, "page": from_page}


def fetch(w):
    errors = []
    for src in w["sources"]:
        try:
            res = PARSERS[src["type"]](src)
            if res:
                return res[0], res[1], src.get("retailer", w["retailer"]), src["url"], ""
            errors.append(f"no price found at {src['url']}")
        except Exception as e:  # network error, 403, bad JSON
            errors.append(f"{src['url']}: {type(e).__name__} {e}")
    return None, None, w["retailer"], w["sources"][0]["url"], "; ".join(errors)[:300]


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
        price, regular, retailer, url, err = fetch(w)
        if price is not None:
            rows.append(dict(ts=ts, id=w["id"], price=f"{price:.2f}", regular=f"{regular:.2f}",
                             retailer=retailer, url=url, source="live", note=""))
            if has_live and price < min(prior):  # first live run only replaces unverified seeds
                alerts.append(f"- **{w['watch']}**: ${price:.2f} at {retailer} (previous low ${min(prior):.2f}). [Buy]({url})")
        else:
            print(f"FAILED {w['id']}: {err}", file=sys.stderr)
        summary.append((w, price, regular, retailer, url, err))
    with HIST.open("w", newline="") as f:
        wr = csv.DictWriter(f, FIELDS)
        wr.writeheader()
        wr.writerows(rows)
    out = []
    for w, price, regular, retailer, url, err in summary:
        hist = [r for r in rows if r["id"] == w["id"] and r["price"]]
        last = hist[-1]
        today = price if price is not None else float(last["price"])
        reg = regular if regular is not None else float(last["regular"] or today)
        earlier = [float(r["price"]) for r in hist if not (price is not None and r is hist[-1])]
        past_low = min(earlier) if earlier else today
        status = "new_low" if today < past_low else "at_low" if today == past_low else "above"
        out.append(dict(id=w["id"], watch=w["watch"], ref=w["ref"], retailer=last["retailer"] if price is None else retailer,
                        today=today, past_low=min(past_low, today), regular=reg,
                        off_regular=round(100 * (reg - today) / reg, 1) if reg else 0,
                        status=status, buy_url=last["url"] if price is None else url,
                        checked=last["ts"], stale=price is None, note=err if price is None else "",
                        history=[[r["ts"], float(r["price"])] for r in hist]))
    (ROOT / "site").mkdir(exist_ok=True)
    (ROOT / "site" / "prices.json").write_text(json.dumps({"updated": ts, "watches": out}, indent=1))
    alert_file = ROOT / "alerts.md"
    alert_file.unlink(missing_ok=True)
    if alerts:
        alert_file.write_text("New lowest price logged:\n\n" + "\n".join(alerts) + "\n")
    Path(os.environ.get("GITHUB_OUTPUT", os.devnull)).open("a").write("ran=true\n")
    print(f"Checked {len(summary)} watches, {sum(1 for s in summary if s[1] is None)} failed, {len(alerts)} alerts.")


if __name__ == "__main__":
    main()

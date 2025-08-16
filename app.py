# app.py
from _future_ import annotations

# ============================== #
#            Imports             #
# ============================== #
from datetime import datetime
from urllib.parse import urljoin, urlparse, quote_plus
from typing import List, Optional, Dict, Union

import json
import re

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query, APIRouter
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, AnyHttpUrl


# ============================== #
#         App Metadata           #
# ============================== #
TAGS_METADATA = [
    {"name": "Health", "description": "Basic health checks."},
    {"name": "Insights", "description": "Fetch structured insights from a Shopify storefront."},
    {"name": "Competitors", "description": "Lightweight competitor discovery and insights."},
]

app = FastAPI(
    title="Shopify Insights (No API)",
    version="0.2.0",
    description=(
        "Reads public Shopify storefront pages (no Shopify Admin API) and returns structured brand intel: "
        "catalog, hero products, policies, FAQs, socials, contact, about, important links, plus basic competitor discovery."
    ),
    openapi_tags=TAGS_METADATA,
    swagger_ui_parameters={
        "defaultModelsExpandDepth": -1,   # hide schemas pane by default
        "docExpansion": "list",           # collapse endpoints
        "displayRequestDuration": True,   # show request durations
    },
)

api = APIRouter()
ui = APIRouter()


# ============================== #
#            Schemas             #
# ============================== #
class Product(BaseModel):
    title: str
    url: Optional[str] = None
    price: Optional[float] = None
    image: Optional[str] = None


class Policy(BaseModel):
    type: str          # privacy/refund/return/shipping/terms
    url: Optional[str] = None
    text_excerpt: Optional[str] = None


class FAQItem(BaseModel):
    question: str
    answer: str
    url: Optional[str] = None


class BrandContext(BaseModel):
    store_url: AnyHttpUrl
    brand_name: Optional[str] = None
    hero_products: List[Product] = []
    catalog: List[Product] = []
    policies: List[Policy] = []
    faqs: List[FAQItem] = []
    social: Dict[str, Optional[str]] = {}
    contact: Dict[str, Optional[Union[List[str], str]]] = {}
    about_text: Optional[str] = None
    important_links: Dict[str, Optional[str]] = {}
    fetched_at: str


class CompetitorResult(BaseModel):
    brand: BrandContext
    competitors: List[BrandContext] = []


# ============================== #
#         Regex & Constants      #
# ============================== #
SOCIAL_MAP = {
    "instagram.com": "instagram",
    "facebook.com": "facebook",
    "x.com": "twitter",
    "twitter.com": "twitter",
    "tiktok.com": "tiktok",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "pinterest.com": "pinterest",
    "linkedin.com": "linkedin",
}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"\+?\d[\d\-\s()]{6,}\d")


# ============================== #
#       Low-level HTTP utils     #
# ============================== #
def text_excerpt(s: str, n: int = 800) -> str:
    s = " ".join((s or "").split())
    return s[:n]


def classify_social(href: str) -> Optional[str]:
    host = urlparse(href).netloc.lower()
    for dom, key in SOCIAL_MAP.items():
        if dom in host:
            return key
    return None


def absolutize(base: str, href: Optional[str]) -> Optional[str]:
    return urljoin(base, href) if href else None


def normalize_root(url: str) -> str:
    """Return scheme+host root, always ending with a slash."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/"


def fetch_html(client: httpx.Client, base: str, path: str) -> Optional[BeautifulSoup]:
    try:
        r = client.get(urljoin(base, path), follow_redirects=True)
        if r.status_code == 200:
            return BeautifulSoup(r.text, "lxml")
    except httpx.RequestError:
        pass
    return None


def fetch_json_ok(client: httpx.Client, url: str) -> Optional[dict]:
    try:
        r = client.get(url, follow_redirects=True)
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None


# ============================== #
#     Parsers & Page Scrapers    #
# ============================== #
def scrape_brand_name(soup: Optional[BeautifulSoup]) -> Optional[str]:
    if not soup:
        return None
    if soup.title and soup.title.text:
        return soup.title.text.strip().split("|")[0].strip()
    og = soup.find("meta", property="og:site_name")
    return og.get("content").strip() if og and og.get("content") else None


def scrape_hero_products(base: str, soup: Optional[BeautifulSoup]) -> List[Product]:
    if not soup:
        return []
    seen, out = set(), []
    for a in soup.select('a[href*="/products/"]'):
        href = absolutize(base, a.get("href"))
        if not href or href in seen:
            continue
        title = (a.get("title") or a.get_text(" ", strip=True) or "").strip()
        if not title:
            img = a.find("img")
            if img and img.get("alt"):
                title = img["alt"].strip()
        if title:
            out.append(Product(title=title, url=href))
            seen.add(href)
        if len(out) >= 8:
            break
    return out


def scrape_catalog(client: httpx.Client, base: str) -> List[Product]:
    products: List[Product] = []
    page = 1
    while True:
        try:
            r = client.get(urljoin(base, f"/products.json?limit=250&page={page}"), follow_redirects=True)
            if r.status_code != 200:
                break
            data = r.json()
            items = data.get("products", [])
            if not items:
                break
            for it in items:
                handle = it.get("handle")
                url = absolutize(base, f"/products/{handle}") if handle else None
                image = None
                if it.get("image") and it["image"].get("src"):
                    image = absolutize(base, it["image"]["src"])
                price = None
                if it.get("variants"):
                    v0 = it["variants"][0]
                    if v0.get("price"):
                        try:
                            price = float(v0["price"])
                        except ValueError:
                            pass
                products.append(Product(title=(it.get("title") or "").strip(), url=url, price=price, image=image))
            page += 1
        except Exception:
            break
    return products


def scrape_policies(client: httpx.Client, base: str) -> List[Policy]:
    paths = [
        ("privacy", "/policies/privacy-policy"),
        ("refund", "/policies/refund-policy"),
        ("return", "/policies/return-policy"),
        ("shipping", "/policies/shipping-policy"),
        ("terms", "/policies/terms-of-service"),
    ]
    out: List[Policy] = []
    for ptype, path in paths:
        soup = fetch_html(client, base, path)
        if soup:
            out.append(Policy(type=ptype, url=urljoin(base, path),
                              text_excerpt=text_excerpt(soup.get_text(" ", strip=True))))
    return out


def scrape_faqs(client: httpx.Client, base: str) -> List[FAQItem]:
    for path in ["/pages/faq", "/pages/faqs", "/pages/help", "/pages/support"]:
        soup = fetch_html(client, base, path)
        if not soup:
            continue
        faqs: List[FAQItem] = []
        # JSON-LD FAQPage
        for s in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(s.text)
                if isinstance(data, dict) and data.get("@type") == "FAQPage":
                    for ent in data.get("mainEntity", []):
                        q = (ent.get("name") or "").strip()
                        a = ""
                        aa = ent.get("acceptedAnswer") or {}
                        if isinstance(aa, dict):
                            a = (aa.get("text") or "").strip()
                        if q and a:
                            faqs.append(FAQItem(question=q, answer=a, url=urljoin(base, path)))
            except Exception:
                pass
        # <details><summary>
        for det in soup.find_all("details"):
            summ = det.find("summary")
            q = (summ.get_text(" ", strip=True) if summ else "").strip()
            a = det.get_text(" ", strip=True)
            if q and a:
                faqs.append(FAQItem(question=q, answer=a, url=urljoin(base, path)))
        if faqs:
            return faqs
    return []


def scrape_social(soup: Optional[BeautifulSoup]) -> Dict[str, Optional[str]]:
    if not soup:
        return {}
    out: Dict[str, Optional[str]] = {}
    for a in soup.find_all("a", href=True):
        key = classify_social(a["href"])
        if key and key not in out:
            out[key] = a["href"]
    return out


def scrape_contact(client: httpx.Client, base: str) -> Dict[str, Optional[Union[List[str], str]]]:
    emails, phones, page_url = [], [], None
    for path in ["/pages/contact", "/pages/contact-us", "/contact"]:
        soup = fetch_html(client, base, path)
        if not soup:
            continue
        txt = soup.get_text(" ", strip=True)
        emails += EMAIL_RE.findall(txt)
        phones += PHONE_RE.findall(txt)
        for a in soup.select('a[href^="mailto:"], a[href^="tel:"]'):
            href = a["href"]
            if href.startswith("mailto:"):
                emails.append(href.replace("mailto:", "").strip())
            if href.startswith("tel:"):
                phones.append(href.replace("tel:", "").strip())
        page_url = urljoin(base, path)
        break
    return {
        "emails": sorted(set(emails)) or None,
        "phones": sorted(set(phones)) or None,
        "contact_page": page_url,
    }


def scrape_about(client: httpx.Client, base: str) -> Optional[str]:
    for path in ["/pages/about", "/pages/our-story", "/pages/about-us"]:
        soup = fetch_html(client, base, path)
        if soup:
            return text_excerpt(soup.get_text(" ", strip=True), 1200)
    return None


def scrape_important_links(client: httpx.Client, base: str) -> Dict[str, Optional[str]]:
    out = {"order_tracking": None, "contact_us": None, "blogs": None}
    for path, key in [
        ("/pages/track", "order_tracking"),
        ("/pages/track-order", "order_tracking"),
        ("/pages/order-tracking", "order_tracking"),
        ("/pages/contact", "contact_us"),
        ("/blogs/news", "blogs"),
        ("/blogs", "blogs"),
    ]:
        soup = fetch_html(client, base, path)
        if soup:
            out[key] = urljoin(base, path)
    return out


# ============================== #
#         Aggregators            #
# ============================== #
def get_brand_context(client: httpx.Client, website_url: str) -> BrandContext:
    base = website_url if website_url.endswith("/") else website_url + "/"
    home = fetch_html(client, base, "/")
    brand_name = scrape_brand_name(home)
    hero_products = scrape_hero_products(base, home)
    catalog = scrape_catalog(client, base)
    policies = scrape_policies(client, base)
    faqs = scrape_faqs(client, base)
    social = scrape_social(home)
    contact = scrape_contact(client, base)
    about_text = scrape_about(client, base)
    important_links = scrape_important_links(client, base)

    return BrandContext(
        store_url=base,
        brand_name=brand_name,
        hero_products=hero_products,
        catalog=catalog,
        policies=policies,
        faqs=faqs,
        social=social,
        contact=contact,
        about_text=about_text,
        important_links=important_links,
        fetched_at=datetime.utcnow().isoformat() + "Z",
    )


# ============================== #
#       Competitor Finder        #
# ============================== #
def looks_like_shopify(client: httpx.Client, url: str) -> bool:
    root = normalize_root(url)
    test_url = urljoin(root, "/products.json?limit=1")
    data = fetch_json_ok(client, test_url)
    return isinstance(data, dict) and "products" in data


def find_competitor_candidates(
    client: httpx.Client,
    website_url: str,
    brand_name: Optional[str],
    limit: int = 3
) -> List[str]:
    root = normalize_root(website_url)
    self_host = urlparse(root).netloc

    queries = []
    if brand_name:
        queries.extend([
            f"{brand_name} shopify",
            f"{brand_name} competitors shopify",
            f"{brand_name} similar brands shopify",
        ])
    else:
        host_without_www = self_host.replace("www.", "")
        queries.extend([
            f"{host_without_www} competitors shopify",
            f"{host_without_www} similar brands shopify",
        ])

    candidates: List[str] = []
    seen_hosts: set[str] = set()
    headers = {"User-Agent": "ShopifyInsightsDemo/1.0"}

    for q in queries:
        url = f"https://duckduckgo.com/html/?q={quote_plus(q)}"
        try:
            r = client.get(url, headers=headers, timeout=15)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "lxml")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if not href.startswith("http"):
                    continue
                host = urlparse(href).netloc
                if not host or host == self_host:
                    continue
                root_cand = normalize_root(href)
                h = urlparse(root_cand).netloc
                if h in seen_hosts:
                    continue
                seen_hosts.add(h)
                candidates.append(root_cand)
                if len(candidates) >= limit * 4:
                    break
        except Exception:
            continue

    filtered: List[str] = []
    for cand in candidates:
        if len(filtered) >= limit:
            break
        try:
            if looks_like_shopify(client, cand):
                filtered.append(cand)
        except Exception:
            continue

    return filtered[:limit]


# ============================== #
#            Routers             #
# ============================== #
@ui.get("/", response_class=HTMLResponse, tags=["Health"])
def home():
    """Simple zero-dependency UI to try the endpoints."""
    return HTMLResponse(
        """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Shopify Insights (No API)</title>
  <style>
    :root { --bg:#0f172a; --card:#111827; --ink:#e5e7eb; --muted:#9ca3af; --accent:#22d3ee; --btn:#1f2937; }
    html,body{margin:0;background:var(--bg);color:var(--ink);font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, Noto Sans, "Apple Color Emoji","Segoe UI Emoji";}
    .wrap{max-width:1000px;margin:32px auto;padding:0 16px;}
    .card{background:var(--card);border-radius:16px;padding:20px;box-shadow:0 10px 30px rgb(0 0 0 / 0.25);}
    h1{font-size:24px;margin:0 0 10px;}
    p{color:var(--muted);margin:0 0 16px;}
    .row{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 16px;}
    input,select{flex:1;min-width:260px;padding:10px 12px;border-radius:12px;border:1px solid #334155;background:#0b1220;color:var(--ink);}
    button{padding:10px 16px;border:1px solid #334155;border-radius:12px;background:var(--btn);color:var(--ink);cursor:pointer}
    button.primary{background:linear-gradient(90deg, #06b6d4, #22d3ee);color:#0b1220;border:none;font-weight:600}
    .cols{display:grid;grid-template-columns:1fr;gap:16px}
    @media (min-width: 960px){ .cols{grid-template-columns: 380px 1fr} }
    pre{white-space: pre-wrap;word-wrap: break-word;background:#0b1220;border-radius:12px;padding:16px;border:1px solid #334155;max-height:70vh;overflow:auto}
    .hint{font-size:12px;color:var(--muted)}
    a{color:var(--accent);text-decoration:none}
    .badge{display:inline-block;background:#0b1220;border:1px solid #334155;border-radius:999px;padding:2px 8px;font-size:12px;margin-left:8px}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>Shopify Insights <span class="badge">no API</span></h1>
      <p>Enter a Shopify storefront URL and get structured brand intel. Or try the competitor finder.</p>
      <div class="cols">
        <div>
          <label>Store URL</label>
          <div class="row">
            <input id="url" placeholder="https://memy.co.in or https://www.gonoise.com" />
          </div>
          <div class="row">
            <button class="primary" onclick="runInsights()">Run /insights</button>
            <select id="limit">
              <option value="3" selected>3 competitors</option>
              <option value="2">2 competitors</option>
              <option value="1">1 competitor</option>
              <option value="4">4 competitors</option>
              <option value="5">5 competitors</option>
            </select>
            <button onclick="runCompetitors()">Run /competitors</button>
          </div>
          <p class="hint">Docs are at <a href="/docs">/docs</a>. Health check at <code>/health</code>.</p>
        </div>
        <div>
          <pre id="out">Output will appear here…</pre>
        </div>
      </div>
    </div>
  </div>
  <script>
    async function runInsights(){
      const u = document.getElementById('url').value.trim();
      if(!u){ return setOut({error:"Please enter a URL"}); }
      setOut("Loading…");
      try{
        const res = await fetch(/insights?website_url=${encodeURIComponent(u)});
        const data = await res.json();
        setOut(data);
      }catch(e){ setOut({error:String(e)}) }
    }
    async function runCompetitors(){
      const u = document.getElementById('url').value.trim();
      const limit = document.getElementById('limit').value;
      if(!u){ return setOut({error:"Please enter a URL"}); }
      setOut("Loading…");
      try{
        const res = await fetch(/competitors?website_url=${encodeURIComponent(u)}&limit=${limit});
        const data = await res.json();
        setOut(data);
      }catch(e){ setOut({error:String(e)}) }
    }
    function setOut(v){
      const el = document.getElementById('out');
      el.textContent = (typeof v === 'string') ? v : JSON.stringify(v, null, 2);
    }
  </script>
</body>
</html>
        """
    )


@api.get("/health", tags=["Health"])
def health():
    return {"status": "ok"}


@api.get("/insights", response_model=BrandContext, tags=["Insights"])
def insights(website_url: AnyHttpUrl = Query(..., description="Shopify store URL, e.g. https://memy.co.in")):
    base = str(website_url)
    client = httpx.Client(timeout=20, headers={"User-Agent": "ShopifyInsightsDemo/1.0"})
    try:
        ctx = get_brand_context(client, base)
        if not ctx.catalog and not ctx.hero_products:
            raise HTTPException(status_code=401, detail="Website not found or not a typical Shopify storefront.")
        return ctx
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")
    finally:
        client.close()


@api.get("/competitors", response_model=CompetitorResult, tags=["Competitors"])
def competitors(
    website_url: AnyHttpUrl = Query(..., description="Brand website (Shopify storefront)"),
    limit: int = Query(3, ge=1, le=5, description="Max competitors to fetch (1–5)")
):
    base = str(website_url)
    client = httpx.Client(timeout=20, headers={"User-Agent": "ShopifyInsightsDemo/1.0"})
    try:
        brand_ctx = get_brand_context(client, base)
        if not brand_ctx.catalog and not brand_ctx.hero_products:
            raise HTTPException(status_code=401, detail="Website not found or not a typical Shopify storefront.")

        competitor_urls = find_competitor_candidates(client, str(brand_ctx.store_url), brand_ctx.brand_name, limit=limit)

        competitor_contexts: List[BrandContext] = []
        for cu in competitor_urls:
            try:
                cctx = get_brand_context(client, cu)
                if cctx.catalog or cctx.hero_products:
                    competitor_contexts.append(cctx)
            except Exception:
                continue

        return CompetitorResult(brand=brand_ctx, competitors=competitor_contexts)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")
    finally:
        client.close()


# Mount routers (UI first so "/" works)
app.include_router(ui)
app.include_router(api)
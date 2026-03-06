from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from datetime import datetime
from collections import defaultdict
import time
import requests

from sklik_api import SklikAPI, SklikAPIError

# ---------------------------------------------------------------
# Zboží.cz parametry kategorií (globální cache)
# ---------------------------------------------------------------
_ZBOZI_PARAMS_SPEC: Optional[Dict[str, List[Dict]]] = None  # categoryText_normalized → [{name, filterGroup}]

def _load_zbozi_params_spec() -> Dict[str, List[Dict]]:
    """Stáhne a zparsuje https://www.zbozi.cz/static/parametry_kategorii.json.
    Vrací dict: normalized_category_path → [{"name": str, "filterGroup": str}, ...]
    """
    global _ZBOZI_PARAMS_SPEC
    if _ZBOZI_PARAMS_SPEC is not None:
        return _ZBOZI_PARAMS_SPEC
    try:
        resp = requests.get(
            "https://www.zbozi.cz/static/parametry_kategorii.json",
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        _ZBOZI_PARAMS_SPEC = {}
        return _ZBOZI_PARAMS_SPEC

    result: Dict[str, List[Dict]] = defaultdict(list)
    for param in data.get("parameters", []):
        param_name = param.get("name", "")
        if not param_name:
            continue
        for cat in param.get("categories", []):
            cat_text = cat.get("categoryText", "")
            filter_group = cat.get("filterGroup", "")
            if cat_text:
                # Normalizujeme oddělovač na " > " a lowercase pro porovnání
                key = cat_text.replace(" | ", " > ").strip().lower()
                result[key].append({"name": param_name, "filterGroup": filter_group})

    _ZBOZI_PARAMS_SPEC = dict(result)
    return _ZBOZI_PARAMS_SPEC


@dataclass
class Recommendation:
    priority: str          # 'critical' | 'important' | 'tip'
    section: str
    title: str
    detail: str
    example: Optional[str] = None
    affected: int = 0


@dataclass
class AnalysisReport:
    shop_id: str
    generated_at: str

    # -- Souhrnne pocty --
    items_total: int = 0
    items_paired: int = 0
    items_errors: int = 0
    items_improvements: int = 0
    items_ok: int = 0

    # -- Vykon celkem (30 dni) --
    perf_views: int = 0
    perf_clicks: int = 0
    perf_cost: float = 0.0
    perf_conversions: int = 0
    perf_avg_cpc: float = 0.0
    perf_ctr: float = 0.0
    perf_conv_rate: float = 0.0
    perf_conv_value: float = 0.0
    perf_pno: float = 0.0  # PNO = náklady / hodnota konverzí * 100

    # -- Recenze --
    reviews_total: int = 0
    reviews_avg_rating: float = 0.0
    reviews_positive: int = 0
    reviews_negative: int = 0
    reviews_list: List[Dict] = field(default_factory=list)
    product_reviews_list: List[Dict] = field(default_factory=list)

    # -- Konkurencni analyza --
    competition_summary: Dict = field(default_factory=dict)
    categories_analysis: List[Dict] = field(default_factory=list)
    items_price_worse: int = 0
    items_price_ok: int = 0
    items_no_delivery: int = 0
    items_no_params: int = 0
    items_no_ean: int = 0

    # -- Feed data (z XML) --
    feed_items_by_id: Dict = field(default_factory=dict)
    feed_quality: Dict = field(default_factory=dict)

    # -- Analýza parametrů dle Zboží.cz spec --
    params_analysis: List[Dict] = field(default_factory=list)  # per kategorie

    # -- Raw data pro dashboard --
    raw_items: List[Dict] = field(default_factory=list)
    raw_stats_daily: List[Dict] = field(default_factory=list)
    raw_diagnostics: Dict = field(default_factory=dict)

    # -- Strukturovane vysledky --
    feed_recommendations: List[Recommendation] = field(default_factory=list)
    sklik_recommendations: List[Recommendation] = field(default_factory=list)
    top_categories_by_clicks: List[Dict] = field(default_factory=list)
    device_stats: List[Dict] = field(default_factory=list)
    feeds_info: List[Dict] = field(default_factory=list)
    campaign_info: Dict = field(default_factory=dict)
    category_params: Dict = field(default_factory=dict)
    items_not_visible: int = 0
    items_without_category: int = 0

    # -- Stav API --
    endpoint_status: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------
class SklikAnalyzer:

    def __init__(self, api: SklikAPI, progress_cb=None):
        self.api = api
        self._progress_cb = progress_cb or (lambda pct, msg: None)

    def _progress(self, pct: int, msg: str):
        self._progress_cb(pct, msg)

    def analyze(self, premise_id: str) -> AnalysisReport:
        report = AnalysisReport(
            shop_id=premise_id,
            generated_at=datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
        )
        self._progress(5, "Ověřuji API klíč…")
        self._fetch_diagnostics(report)
        self._progress(10, "Načítám diagnostiku feedu…")
        self._fetch_feeds(report)
        self._progress(15, "Stahuji XML feed (může trvat)…")
        self._fetch_feed_content(report)
        self._progress(25, "Načítám položky katalogu…")
        self._fetch_items(report)
        self._enrich_items_from_feed(report)
        self._fetch_category_names(report)
        self._progress(30, "Stahuji konkurenční data…")
        self._fetch_product_details(report)
        self._progress(75, "Stav kampaně…")
        self._fetch_campaign(report)
        self._progress(80, "Statistiky – denní data…")
        self._fetch_stats_aggregated(report)
        self._progress(85, "Statistiky kategorií…")
        self._fetch_stats_category(report)
        self._progress(90, "Recenze zákazníků…")
        self._fetch_reviews(report)
        self._fetch_category_params(report)
        self._progress(95, "Generuji analýzu a doporučení…")

        self._analyze_feed_quality(report)
        self._analyze_categories(report)
        self._analyze_params_spec(report)
        self._build_feed_recommendations(report)
        self._build_sklik_recommendations(report)
        self._progress(100, "Hotovo!")
        return report

    # ---------------------------------------------------------------
    # Safe API call
    # ---------------------------------------------------------------

    def _safe(self, name: str, report: AnalysisReport, fn):
        try:
            result = fn()
            report.endpoint_status[name] = "ok"
            return result
        except SklikAPIError as e:
            report.endpoint_status[name] = str(e)
            report.warnings.append(f"{name}: {e}")
            return None
        except Exception as e:
            report.endpoint_status[name] = f"Chyba: {e}"
            report.warnings.append(f"{name}: {e}")
            return None

    # ---------------------------------------------------------------
    # Numeric utils
    # ---------------------------------------------------------------

    @staticmethod
    def _num(val) -> float:
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, dict):
            nums = [v for v in val.values() if isinstance(v, (int, float))]
            return float(sum(nums)) if nums else 0.0
        return 0.0

    def _m(self, row: dict, *keys) -> float:
        for k in keys:
            if k in row:
                return self._num(row[k])
        return 0.0

    @staticmethod
    def _halere(val: float, total_clicks: float) -> float:
        if total_clicks > 0 and val / total_clicks > 1000:
            return val / 100.0
        return val

    # ---------------------------------------------------------------
    # Fetching – Diagnostika
    # ---------------------------------------------------------------

    def _fetch_diagnostics(self, report: AnalysisReport):
        data = self._safe("diagnostics", report, self.api.get_diagnostics)
        if not data:
            return
        d = data if isinstance(data, dict) else {}
        report.raw_diagnostics = d
        report.items_total = int(d.get("total", 0))
        report.items_ok = int(d.get("ok", 0))
        report.items_errors = int(d.get("error", 0))
        report.items_improvements = int(d.get("canBeImproved", 0))
        report.items_not_visible = int(d.get("notVisible", 0))
        report.items_without_category = int(d.get("withoutCategory", 0))

    # ---------------------------------------------------------------
    # Fetching – Položky (cursor-based pagination)
    # ---------------------------------------------------------------

    def _normalize_item(self, item: dict) -> dict:
        product = item.get("product")
        paired = (product is not None and isinstance(product, dict)) or bool(item.get("matchingId"))

        product_id = None
        top_position = None
        from_cheapest_position = None
        cat_id = item.get("categoryId")

        if product and isinstance(product, dict):
            product_id = product.get("productId")
            if not cat_id:
                cat_id = product.get("categoryId")
            pdi = product.get("productDetailInfo") or {}
            top_position = pdi.get("topPosition")
            from_cheapest_position = pdi.get("fromCheapestPosition")

        # matchingId v basic items IS the productId (paired product catalog ID)
        if not product_id:
            mid = item.get("matchingId")
            if mid and str(mid) not in ("", "feed"):
                product_id = mid

        max_cpc = item.get("maxCpcSearch")
        price = item.get("price")
        if price is None and product and isinstance(product, dict):
            price = product.get("price")
        if price is not None:
            price = float(price)

        delivery_date = item.get("deliveryDate")
        has_delivery = delivery_date is not None
        has_params = bool(item.get("params") or item.get("parameters"))
        has_ean = bool(item.get("ean"))

        search_info = item.get("searchInfo") or {}
        suggested_cpc = search_info.get("suggestedCpc")
        if suggested_cpc is not None:
            suggested_cpc = float(suggested_cpc)

        return {
            "id": str(item.get("itemId") or ""),
            "name": item.get("name") or "—",
            "productName": item.get("name") or "",
            "price": price,
            "paired": paired,
            "productId": product_id,
            "category": "",
            "categoryPath": "",
            "categoryId": cat_id,
            "manufacturer": str(item.get("manufacturerId") or ""),
            "delivery": delivery_date,
            "hasDelivery": has_delivery,
            "hasParams": has_params,
            "hasEan": has_ean,
            "maxCpc": float(max_cpc) if max_cpc is not None else None,
            "topPosition": top_position,
            "fromCheapestPosition": from_cheapest_position,
            "url": item.get("url") or "",
            "img": item.get("imgUrl") or "",
            "shopCount": None,
            "minPrice": None,
            "maxPrice": None,
            "priceVsMin": None,
            "suggestedCpc": suggested_cpc,
            "topRank": top_position,
            "productRating": None,
            "productRatingCount": None,
        }

    def _fetch_items(self, report: AnalysisReport):
        all_items = []

        # 1) Try basic items (no detail) – integer offset pagination
        # Fénix API /nakupy/shop-items/ vrací {"items": [...], "totalItems": N}
        # NE {"data": [...], "totalCount": N}
        data = self._safe("items_basic", report, lambda: self.api.get_items_basic(limit=3000))
        if data and isinstance(data.get("items"), list):
            all_items = data["items"]
            total = data.get("totalItems", len(all_items))
            # Offset pagination: integer offset (ne opaque cursor)
            next_offset = len(all_items)
            while len(all_items) < total and next_offset < total:
                cur_offset = next_offset
                page = self._safe(f"items_p{len(all_items)}", report,
                                  lambda o=cur_offset: self.api.get_items_basic(limit=3000, offset=o))
                if not page or not isinstance(page.get("items"), list) or not page["items"]:
                    break
                all_items.extend(page["items"])
                next_offset = len(all_items)

        # 1b) Fetch suggestedCpc via loadSearchInfo=True (max 300/req)
        if all_items:
            search_info_map = {}
            si_offset = 0
            while si_offset < len(all_items):
                si_data = None
                for _retry in range(5):
                    try:
                        si_data = self.api.get_items(
                            limit=300, offset=si_offset,
                            load_product_detail=False,
                            load_search_info=True,
                        )
                        break
                    except SklikAPIError as e:
                        if "429" in str(e) or "Rate limit" in str(e):
                            time.sleep(10 * (_retry + 1))
                        else:
                            report.warnings.append(f"items_si_{si_offset}: {e}")
                            break
                    except Exception as e:
                        report.warnings.append(f"items_si_{si_offset}: {e}")
                        break
                if not si_data:
                    break
                si_batch = si_data.get("items") or si_data.get("data") or []
                if not isinstance(si_batch, list) or not si_batch:
                    break
                for si in si_batch:
                    iid = str(si.get("itemId", ""))
                    # Dle OpenAPI spec: pole je maxCpcSearch přímo na ShopItemResponse
                    # (loadSearchInfo=True přidává searchDataDatetime, ale CPC je maxCpcSearch)
                    cpc = si.get("maxCpcSearch")
                    if cpc is None:
                        # fallback: starší/jiná verze API
                        si_info = si.get("searchInfo") or {}
                        cpc = si_info.get("suggestedCpc") or si_info.get("maxCpc")
                    if iid and cpc is not None:
                        search_info_map[iid] = float(cpc)
                si_offset += len(si_batch)
                if len(si_batch) < 300:
                    break
            # Doplníme suggestedCpc do basic items
            if search_info_map:
                for item in all_items:
                    iid = str(item.get("itemId", ""))
                    if iid in search_info_map:
                        if "searchInfo" not in item:
                            item["searchInfo"] = {}
                        item["searchInfo"]["suggestedCpc"] = search_info_map[iid]

        # 2) Fallback: items with detail (limit 30)
        if not all_items:
            next_offset = None
            for _ in range(10):  # max 300 items
                data = self._safe(f"items_detail_{len(all_items)}", report,
                                  lambda o=next_offset: self.api.get_items(
                                      limit=30, offset=o,
                                      load_product_detail=False,
                                      load_search_info=False))
                # Fallback endpoint může vracet "items" nebo "data" – zkusíme obě
                if not data:
                    break
                batch = data.get("items") or data.get("data")
                if not isinstance(batch, list) or not batch:
                    break
                all_items.extend(batch)
                next_offset = data.get("offset") or len(all_items)
                if len(batch) < 30:
                    break

        # 3) Fill from feed
        if report.feed_items_by_id:
            api_ids = {str(item.get("itemId", "")) for item in all_items}
            for fid, fdata in report.feed_items_by_id.items():
                if fid not in api_ids:
                    all_items.append({
                        "itemId": fid,
                        "name": fdata.get("productName", ""),
                        "categoryId": None,
                        "condition": "new",
                        "matchingId": "feed",
                        "from_feed": True,
                    })

        report.items_total = max(report.items_total, len(all_items))
        report.endpoint_status["items"] = f"ok ({len(all_items)} položek)"

        paired = 0
        normalized = []
        for item in all_items:
            n = self._normalize_item(item)
            if n["paired"]:
                paired += 1
            normalized.append(n)

        report.items_paired = paired
        report.items_no_delivery = sum(1 for n in normalized if not n["hasDelivery"])
        report.items_no_params = sum(1 for n in normalized if not n["hasParams"])
        report.items_no_ean = sum(1 for n in normalized if not n["hasEan"])
        report.raw_items = normalized

    # ---------------------------------------------------------------
    # Fetching – Product details (competitive data)
    # ---------------------------------------------------------------

    def _fetch_category_names(self, report: AnalysisReport):
        """Načte názvy všech kategorií z API a doplní je do raw_items."""
        cat_ids = []
        seen = set()
        for item in report.raw_items:
            cid = item.get("categoryId")
            if cid and cid not in seen:
                seen.add(cid)
                cat_ids.append(cid)

        if not cat_ids:
            return

        cat_name_map = {}
        for i in range(0, len(cat_ids), 10):
            batch = cat_ids[i:i+10]
            data = self._safe(
                f"cat_names_{i//10}", report,
                lambda b=batch: self.api.get_categories(b)
            )
            if not data:
                continue
            # Fénix API vrací {"items": [...], "meta": {...}}
            cats = data.get("items") or data.get("data", data) if isinstance(data, dict) else data
            if isinstance(cats, list):
                for cat in cats:
                    cid = str(cat.get("categoryId") or cat.get("id") or "")
                    path = cat.get("path") or []
                    if isinstance(path, list) and path:
                        name = str(path[-1])
                        full_path = " > ".join(str(p) for p in path)
                    else:
                        name = cat.get("categoryText") or cat.get("name") or cat.get("text") or ""
                        full_path = name
                    if cid and name:
                        cat_name_map[cid] = {"name": name, "fullPath": full_path}

        for item in report.raw_items:
            if not item.get("category"):
                cid = str(item.get("categoryId") or "")
                if cid in cat_name_map:
                    item["category"] = cat_name_map[cid]["name"]
                    item["categoryPath"] = cat_name_map[cid]["fullPath"]

    def _fetch_product_details(self, report: AnalysisReport):
        product_ids = []
        seen = set()
        for item in report.raw_items:
            pid = item.get("productId")
            if pid and pid not in seen:
                seen.add(pid)
                product_ids.append(pid)

        if not product_ids:
            return

        # Stratifikovaný sampling (max 500) – stejná logika jako v zbozi-analyzer
        MAX_IDS = 500
        if len(product_ids) > MAX_IDS:
            from collections import defaultdict as _dd
            pid_meta = {}
            for item in report.raw_items:
                pid = item.get("productId")
                if pid and pid not in pid_meta:
                    pid_meta[pid] = {
                        "shopCount": item.get("shopCount") or 0,
                        "categoryId": item.get("categoryId") or "unknown",
                    }
            with_comp = [p for p in product_ids if pid_meta.get(p, {}).get("shopCount", 0) > 1]
            without_comp = [p for p in product_ids if p not in set(with_comp)]
            by_cat = _dd(list)
            for pid in with_comp:
                cat = pid_meta.get(pid, {}).get("categoryId", "unknown")
                by_cat[cat].append(pid)
            for cat in by_cat:
                by_cat[cat].sort(key=lambda p: pid_meta.get(p, {}).get("shopCount", 0), reverse=True)
            total_with = len(with_comp)
            slots = min(MAX_IDS, total_with)
            selected = []
            if total_with > 0:
                cats = list(by_cat.keys())
                remaining = slots
                for idx, cat in enumerate(cats):
                    if idx == len(cats) - 1:
                        alloc = remaining
                    else:
                        alloc = max(1, min(round(slots * len(by_cat[cat]) / total_with), remaining - (len(cats) - idx - 1)))
                    selected.extend(by_cat[cat][:alloc])
                    remaining -= min(alloc, len(by_cat[cat]))
            rem = MAX_IDS - len(selected)
            if rem > 0:
                selected.extend(without_comp[:rem])
            product_ids = selected

        product_data = {}
        total_batches = (len(product_ids) + 9) // 10
        for i in range(0, len(product_ids), 10):
            batch_num = i // 10 + 1
            pct = 30 + int(45 * batch_num / total_batches)
            self._progress(pct, f"Konkurenční data: batch {batch_num}/{total_batches}…")
            batch = product_ids[i:i+10]
            data = self._safe(
                f"products_batch_{i//10}",
                report,
                lambda b=batch: self.api.get_products(b)
            )
            if not data:
                continue
            # Fénix API vrací {"items": [...]} – stejná struktura jako shop-items
            products = data.get("items") or data.get("data") or (data if isinstance(data, list) else [])

            own_shop_id = int(self.api.premise_id) if self.api.premise_id else None

            def _extract_product(p):
                pid = p.get("productId") or p.get("id")
                if pid:
                    shop_items = p.get("shopItems") or []
                    competitor_prices = []
                    own_price = None
                    for si in shop_items:
                        # Fénix API používá "premiseId" (ne "shopId")
                        si_shop_id = si.get("premiseId") or si.get("shopId")
                        si_price = si.get("price")
                        if si_price is not None:
                            if si_shop_id == own_shop_id:
                                own_price = float(si_price)
                            else:
                                competitor_prices.append(float(si_price))
                    min_competitors = min(competitor_prices) if competitor_prices else None

                    product_data[pid] = {
                        "shopCount": p.get("shopCount"),
                        "minPrice": p.get("minPrice"),
                        "maxPrice": p.get("maxPrice"),
                        "productName": p.get("productName") or p.get("name"),
                        "categoryId": p.get("categoryId"),
                        "categoryName": p.get("categoryName"),
                        "minPriceCompetitors": min_competitors,
                        "ownPrice": own_price,
                    }

            if isinstance(products, list):
                for p in products:
                    _extract_product(p)
            elif isinstance(products, dict) and "shopCount" in products:
                _extract_product(products)

        for item in report.raw_items:
            pid = item.get("productId")
            if pid and pid in product_data:
                pd = product_data[pid]
                item["shopCount"] = pd["shopCount"]
                item["minPrice"] = pd["minPrice"]
                item["maxPrice"] = pd["maxPrice"]
                # Cena našeho shopu z shopItems (basic items ji neobsahují přímo)
                if item.get("price") is None and pd.get("ownPrice") is not None:
                    item["price"] = pd["ownPrice"]
                item["minPriceCompetitors"] = pd.get("minPriceCompetitors")
                if pd.get("productName"):
                    item["productName"] = pd["productName"]
                my_price = float(item["price"]) if item.get("price") else None
                min_comp = float(pd["minPriceCompetitors"]) if pd.get("minPriceCompetitors") and pd["minPriceCompetitors"] > 0 else None
                min_p = min_comp if min_comp is not None else (float(pd["minPrice"]) if pd.get("minPrice") and pd["minPrice"] > 0 else None)
                if my_price and min_p:
                    item["priceVsMin"] = round(my_price / min_p, 3)
                    if my_price > min_p * 1.05:
                        item["recommendedPrice"] = round(min_p, 0)
                        item["priceSavings"] = round(my_price - min_p, 0)
                    elif my_price <= min_p:
                        item["priceAdvantage"] = round(min_p - my_price, 0)

        report.items_price_worse = sum(1 for n in report.raw_items if n.get("priceVsMin") and n["priceVsMin"] > 1.05)
        report.items_price_ok = sum(1 for n in report.raw_items if n.get("priceVsMin") and n["priceVsMin"] <= 1.05)

    # ---------------------------------------------------------------
    # Fetching – Feeds, Campaign
    # ---------------------------------------------------------------

    def _fetch_feeds(self, report: AnalysisReport):
        data = self._safe("feeds", report, self.api.get_feeds)
        if not data:
            return
        # Fénix API vrací {"items": [...]} stejně jako ostatní endpointy
        feeds = data.get("items") or data.get("data", data)

        def _fmt_ts(f):
            ts = f.get("lastSuccessfulImport")
            if isinstance(ts, (int, float)) and ts > 0:
                f["lastSuccessfulImportFormatted"] = datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")
            elif isinstance(ts, str) and ts:
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    f["lastSuccessfulImportFormatted"] = dt.strftime("%d.%m.%Y %H:%M")
                except Exception:
                    f["lastSuccessfulImportFormatted"] = ts[:16]

        if isinstance(feeds, list):
            for f in feeds:
                _fmt_ts(f)
            report.feeds_info = feeds
        elif isinstance(feeds, dict):
            _fmt_ts(feeds)
            report.feeds_info = [feeds]

    def _fetch_feed_content(self, report: AnalysisReport):
        if not report.feeds_info:
            return
        for feed in report.feeds_info:
            feed_url = feed.get("feedUrl") or feed.get("url")
            if not feed_url:
                continue
            try:
                feed_items = self.api.download_feed(feed_url)
                for fi in feed_items:
                    iid = fi.get("itemId")
                    if iid:
                        report.feed_items_by_id[str(iid)] = fi
                report.endpoint_status["feed_download"] = f"ok ({len(feed_items)} položek)"
            except Exception as e:
                report.endpoint_status["feed_download"] = str(e)
                report.warnings.append(f"feed_download: {e}")
            break

    def _enrich_items_from_feed(self, report: AnalysisReport):
        if not report.feed_items_by_id:
            return
        for item in report.raw_items:
            fi = report.feed_items_by_id.get(item["id"])
            if not fi:
                continue
            if item["price"] is None and fi.get("price") is not None:
                item["price"] = fi["price"]
            if not item["hasDelivery"] and fi.get("deliveryDate") is not None:
                item["delivery"] = fi["deliveryDate"]
                item["hasDelivery"] = True
            if not item["hasEan"] and fi.get("ean"):
                item["hasEan"] = True
            if not item["hasParams"] and fi.get("params"):
                item["hasParams"] = bool(fi["params"])
            if not item["url"] and fi.get("url"):
                item["url"] = fi["url"]
            if not item["img"] and fi.get("imgUrl"):
                item["img"] = fi["imgUrl"]
            if not item["category"] and fi.get("categoryText"):
                item["category"] = fi["categoryText"]
            if (not item["manufacturer"] or item["manufacturer"] == "") and fi.get("manufacturer"):
                item["manufacturer"] = fi["manufacturer"]

        report.items_no_delivery = sum(1 for n in report.raw_items if not n["hasDelivery"])
        report.items_no_params = sum(1 for n in report.raw_items if not n["hasParams"])
        report.items_no_ean = sum(1 for n in report.raw_items if not n["hasEan"])

    def _fetch_campaign(self, report: AnalysisReport):
        data = self._safe("campaign", report, self.api.get_campaigns)
        if not data:
            return
        # Fenix returns list of campaigns
        campaigns = data.get("data", data) if isinstance(data, dict) else data
        if isinstance(campaigns, list) and campaigns:
            # Use first active campaign
            report.campaign_info = campaigns[0] if len(campaigns) == 1 else {"campaigns": campaigns}
        elif isinstance(campaigns, dict):
            report.campaign_info = campaigns

    # ---------------------------------------------------------------
    # Fetching – Statistics (POST)
    # ---------------------------------------------------------------

    def _fetch_stats_aggregated(self, report: AnalysisReport):
        data = self._safe("stats_item", report, lambda: self.api.get_stats_item(30))
        if not data:
            return
        try:
            # Diagnostika: logujeme co API vrátilo
            debug_info = data.get("_debug", "")
            raw_keys = data.get("_raw_keys", [])
            rows = data.get("data", [])
            if not isinstance(rows, list) or not rows:
                report.warnings.append(
                    f"stats_item: prázdná data. Klíče reportu: {raw_keys or list(data.keys())}. "
                    f"{('Debug: ' + debug_info) if debug_info else ''} Vzorek: {str(data)[:400]}"
                )
                return

            views = sum(self._m(r, "impressions") for r in rows)
            clicks = sum(self._m(r, "clicks") for r in rows)
            # Fénix API vrací náklady jako "totalMoney" (ne "cost")
            cost_raw = sum(self._m(r, "totalMoney", "cost") for r in rows)
            convs = sum(self._m(r, "conversions", "directConversions") for r in rows)
            # Hodnota konverzí – Fénix pole: conversionValue, directConversionValue nebo orderValue
            conv_value_raw = sum(self._m(r, "conversionValue", "directConversionValue", "orderValue") for r in rows)

            cost = self._halere(cost_raw, clicks)
            # conversionValue může být v haléřích stejně jako totalMoney
            conv_value = self._halere(conv_value_raw, max(convs, 1))

            report.perf_views = int(views)
            report.perf_clicks = int(clicks)
            report.perf_cost = round(cost, 2)
            report.perf_conversions = int(convs)
            report.perf_conv_value = round(conv_value, 2)
            if cost > 0 and conv_value > 0:
                report.perf_pno = round(cost / conv_value * 100, 2)
            if clicks > 0:
                report.perf_avg_cpc = round(cost / clicks, 2)
            if views > 0:
                report.perf_ctr = round(clicks / views * 100, 2)
            if clicks > 0:
                report.perf_conv_rate = round(convs / clicks * 100, 2)

            daily = []
            for r in rows:
                # Fénix vrací datum jako string "YYYY-MM-DD" v poli "date"
                ts = r.get("date") or r.get("startTimestamp") or r.get("dateFrom")
                if isinstance(ts, (int, float)):
                    date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                elif isinstance(ts, str) and len(ts) >= 10:
                    date_str = ts[:10]
                else:
                    date_str = str(ts or "")
                rc = self._m(r, "totalMoney", "cost")
                r_clicks = max(self._m(r, "clicks"), 1)
                daily.append({
                    "date": date_str,
                    "views": int(self._m(r, "impressions")),
                    "clicks": int(self._m(r, "clicks")),
                    "cost": round(self._halere(rc, r_clicks), 2),
                    "conversions": int(self._m(r, "conversions", "directConversions")),
                })
            report.raw_stats_daily = daily
        except Exception as e:
            report.warnings.append(f"stats_item: {e}")

    def _fetch_stats_category(self, report: AnalysisReport):
        data = self._safe("stats_category", report, lambda: self.api.get_stats_category(30))
        if not data:
            return
        try:
            rows = data.get("data", [])
            if not isinstance(rows, list):
                return
            for row in rows:
                path = row.get("path")
                if isinstance(path, list) and path:
                    row["categoryName"] = " > ".join(str(p) for p in path)
                    row["categoryShortName"] = str(path[-1])
                # offerCategory je přímý textový název kategorie z Fénix statistik
                offer_cat = row.get("offerCategory")
                if offer_cat and not row.get("categoryName"):
                    row["categoryName"] = str(offer_cat)
                    row["categoryShortName"] = str(offer_cat)
                # Normalizace: totalMoney → cost alias pro _analyze_categories
                if "totalMoney" in row and "cost" not in row:
                    row["cost"] = row["totalMoney"]
                # Normalizace: impressions → views alias
                if "impressions" in row and "views" not in row:
                    row["views"] = row["impressions"]
            sorted_rows = sorted(rows, key=lambda x: self._m(x, "clicks"), reverse=True)
            report.top_categories_by_clicks = sorted_rows[:20]
        except Exception as e:
            report.warnings.append(f"stats_category: {e}")

    # ---------------------------------------------------------------
    # Fetching – Reviews
    # ---------------------------------------------------------------

    def _fetch_reviews(self, report: AnalysisReport):
        data = self._safe("reviews", report, self.api.get_reviews)
        if data:
            try:
                # Fénix API /nakupy/reviews/ vrací {"items": [...], "totalItems": N}
                # NE {"data": [...], "totalCount": N}
                rows = data.get("items") or data.get("data") or []
                if isinstance(rows, list) and rows:
                    report.reviews_total = (
                        data.get("totalItems") or data.get("totalCount") or len(rows)
                    )
                    ratings = []
                    pos = neg = 0
                    normalized_reviews = []
                    for r in rows:
                        satisfaction = r.get("satisfaction") or {}
                        overall = satisfaction.get("overall") if isinstance(satisfaction, dict) else None
                        score = None
                        if overall == "yes":
                            score = 5.0
                            pos += 1
                        elif overall == "yes_but":
                            score = 3.0
                        elif overall == "no":
                            score = 1.0
                            neg += 1
                        if score is not None:
                            ratings.append(score)

                        normalized_reviews.append({
                            "satisfaction": satisfaction,
                            "overall": overall,
                            "score": score,
                            "positiveComment": r.get("positiveComment") or "",
                            "negativeComment": r.get("negativeComment") or "",
                            "userName": r.get("userName") or "Zákazník",
                            "createTimestamp": r.get("createTimestamp"),
                            "orderId": r.get("orderId"),
                        })

                    if ratings:
                        report.reviews_avg_rating = round(sum(ratings) / len(ratings), 2)
                    report.reviews_positive = pos
                    report.reviews_negative = neg
                    report.reviews_list = normalized_reviews[:20]
            except Exception as e:
                report.warnings.append(f"reviews parsing: {e}")

        data2 = self._safe("product_reviews", report, self.api.get_product_reviews)
        if data2:
            try:
                # Stejná struktura jako reviews: "items" / "totalItems"
                rows2 = data2.get("items") or data2.get("data") or []
                if isinstance(rows2, list):
                    normalized_prod = []
                    for r in rows2:
                        pd = r.get("productData") or {}
                        normalized_prod.append({
                            "ratingStars": r.get("ratingStars"),
                            "text": r.get("text") or "",
                            "positiveComments": r.get("positiveComments") or "",
                            "negativeComments": r.get("negativeComments") or "",
                            "productName": pd.get("productName") or "",
                            "itemId": pd.get("itemId"),
                        })
                    report.product_reviews_list = normalized_prod[:20]
            except Exception as e:
                report.warnings.append(f"product_reviews parsing: {e}")

    # ---------------------------------------------------------------
    # Fetching – Category params
    # ---------------------------------------------------------------

    def _fetch_category_params(self, report: AnalysisReport):
        cat_ids = []
        seen = set()
        for item in report.raw_items:
            cid = item.get("categoryId")
            if cid and cid not in seen:
                seen.add(cid)
                cat_ids.append(cid)
            if len(cat_ids) >= 10:
                break

        if not cat_ids:
            return

        data = self._safe("category_params", report, lambda: self.api.get_categories(cat_ids))
        if not data:
            return
        try:
            cats = data.get("data", data) if isinstance(data, dict) else data
            if isinstance(cats, list):
                for cat in cats:
                    cid = str(cat.get("id") or cat.get("categoryId") or "")
                    attrs = cat.get("attributes") or cat.get("params") or cat.get("parameters") or []
                    if cid and attrs:
                        report.category_params[cid] = attrs
            elif isinstance(cats, dict):
                report.category_params = {str(k): v for k, v in cats.items()}
        except Exception as e:
            report.warnings.append(f"category_params: {e}")

    # ---------------------------------------------------------------
    # Analysis
    # ---------------------------------------------------------------

    def _analyze_feed_quality(self, report: AnalysisReport):
        if not report.feed_items_by_id:
            return

        feed_items = list(report.feed_items_by_id.values())
        total = len(feed_items)

        has = {
            "price": 0, "ean": 0, "deliveryDate": 0, "imgUrl": 0,
            "categoryText": 0, "manufacturer": 0, "params": 0,
            "extraMessage": 0, "priceBeforeDiscount": 0, "salesVoucher": 0,
            "warranty": 0, "maxCpc": 0,
        }
        premium_items = []
        gift_set_items = []

        for fi in feed_items:
            if fi.get("price") is not None: has["price"] += 1
            if fi.get("ean"): has["ean"] += 1
            if fi.get("deliveryDate") is not None: has["deliveryDate"] += 1
            if fi.get("imgUrl"): has["imgUrl"] += 1
            if fi.get("categoryText"): has["categoryText"] += 1
            if fi.get("manufacturer"): has["manufacturer"] += 1
            if fi.get("params"): has["params"] += 1

            price = fi.get("price") or 0
            name = (fi.get("productName") or "").lower()

            if price >= 2000:
                premium_items.append(fi)
            if any(kw in name for kw in ["dárk", "gift", "set ", "sada", "kazeta"]):
                gift_set_items.append(fi)

        extra_message_recs = []
        extra_message_recs.append({
            "message": "free_return",
            "label": "Vrácení s dopravou zdarma",
            "count": total,
            "reason": "Pokud eshop nabízí bezplatné vrácení, zvýší to konverzi.",
        })
        if gift_set_items:
            extra_message_recs.append({
                "message": "gift_package",
                "label": "Dárkové balení",
                "count": len(gift_set_items),
                "reason": f"{len(gift_set_items)} produktů jsou dárkové sady/sety.",
            })
        if premium_items:
            extra_message_recs.append({
                "message": "split_payment",
                "label": "Možnost nákupu na splátky",
                "count": len(premium_items),
                "reason": f"{len(premium_items)} produktů stojí nad 2 000 Kč.",
            })
        extra_message_recs.append({
            "message": "voucher",
            "label": "Voucher na další nákup",
            "count": total,
            "reason": "Slevový kód na příští nákup zvyšuje opakované konverze.",
        })
        extra_message_recs.append({
            "message": "free_gift",
            "label": "Dárek zdarma (vzorky)",
            "count": total,
            "reason": "Vzorky/dárky k objednávce – silný konverzní faktor.",
        })

        report.feed_quality = {
            "total": total,
            "has": has,
            "missing_extra_message": total,
            "missing_params": total - has["params"],
            "missing_price_before_discount": total,
            "missing_warranty": total,
            "missing_max_cpc": total,
            "premium_items_count": len(premium_items),
            "gift_set_count": len(gift_set_items),
            "extra_message_recommendations": extra_message_recs,
        }

    def _analyze_categories(self, report: AnalysisReport):
        # Sestavíme mapu categoryId → offerCategory z statistik, pokud chybí název z feedu
        # offerCategory je textový název z Fénix /nakupy/statistics/item řádků
        offer_cat_by_id: Dict[str, str] = {}
        for row in report.top_categories_by_clicks:
            cid = str(row.get("categoryId") or "")
            offer_cat = row.get("offerCategory") or row.get("categoryName") or ""
            if cid and offer_cat:
                offer_cat_by_id[cid] = offer_cat

        cat_agg: Dict[str, dict] = defaultdict(lambda: {
            "category": "",
            "categoryId": None,
            "items": 0,
            "paired": 0,
            "shopCounts": [],
            "priceVsMinList": [],
            "suggestedCpcs": [],
            "maxCpcs": [],
            "noDelivery": 0,
            "noParams": 0,
            "noEan": 0,
        })

        for item in report.raw_items:
            # Doplnění názvu kategorie z offerCategory statistik pokud chybí z feedu
            if not item.get("category") and item.get("categoryId"):
                cid_str = str(item["categoryId"])
                if cid_str in offer_cat_by_id:
                    item["category"] = offer_cat_by_id[cid_str]

            cat = item.get("category") or "Bez kategorie"
            cid = item.get("categoryId")
            a = cat_agg[cat]
            a["category"] = cat
            if cid:
                a["categoryId"] = cid
            a["items"] += 1
            if item.get("paired"):
                a["paired"] += 1
            if item.get("shopCount"):
                a["shopCounts"].append(float(item["shopCount"]))
            if item.get("priceVsMin"):
                a["priceVsMinList"].append(float(item["priceVsMin"]))
            if item.get("suggestedCpc"):
                a["suggestedCpcs"].append(float(item["suggestedCpc"]))
            if item.get("maxCpc"):
                a["maxCpcs"].append(float(item["maxCpc"]))
            if not item.get("hasDelivery"):
                a["noDelivery"] += 1
            if not item.get("hasParams"):
                a["noParams"] += 1
            if not item.get("hasEan"):
                a["noEan"] += 1

        stat_by_cat: Dict[str, dict] = {}
        for row in report.top_categories_by_clicks:
            cat_name = row.get("categoryName") or row.get("categoryShortName") or ""
            cat_id = str(row.get("categoryId") or "")
            key = cat_name or cat_id
            if key:
                stat_by_cat[key] = row
                if cat_id:
                    stat_by_cat[cat_id] = row

        def avg(lst):
            return round(sum(lst) / len(lst), 2) if lst else None

        result = []
        for cat, a in cat_agg.items():
            stats = stat_by_cat.get(cat) or stat_by_cat.get(str(a.get("categoryId") or "")) or {}
            clicks = int(self._m(stats, "clicks"))
            # Fénix vrací zobrazení jako "impressions"; "views" je alias přidaný v _fetch_stats_category
            views = int(self._m(stats, "views", "impressions"))
            # Fénix vrací náklady jako "totalMoney"; "cost" je alias přidaný v _fetch_stats_category
            cost = self._m(stats, "cost", "totalMoney")
            convs = int(self._m(stats, "conversions", "directConversions"))

            avg_shop_count = avg(a["shopCounts"])
            avg_price_ratio = avg(a["priceVsMinList"])
            avg_suggested_cpc = avg(a["suggestedCpcs"])
            avg_max_cpc = avg(a["maxCpcs"])

            cpc_gap = None
            if avg_suggested_cpc and avg_max_cpc:
                cpc_gap = round(avg_suggested_cpc - avg_max_cpc, 2)

            ctr = round(clicks / views * 100, 2) if views > 0 else None

            result.append({
                "category": cat,
                "categoryId": a["categoryId"],
                "items": a["items"],
                "paired": a["paired"],
                "pairedPct": round(a["paired"] / a["items"] * 100) if a["items"] > 0 else 0,
                "avgShopCount": avg_shop_count,
                "avgPriceVsMin": avg_price_ratio,
                "avgSuggestedCpc": avg_suggested_cpc,
                "avgMaxCpc": avg_max_cpc,
                "cpcGap": cpc_gap,
                "noDelivery": a["noDelivery"],
                "noParams": a["noParams"],
                "noEan": a["noEan"],
                "clicks": clicks,
                "views": views,
                "ctr": ctr,
                "cost": round(self._halere(cost, max(clicks, 1)), 2),
                "conversions": convs,
            })

        result.sort(key=lambda x: (-(x["clicks"] or 0), -x["items"]))
        report.categories_analysis = result

        all_shop_counts = [item["shopCount"] for item in report.raw_items if item.get("shopCount")]
        all_price_ratios = [item["priceVsMin"] for item in report.raw_items if item.get("priceVsMin")]
        cheapest = [i for i in report.raw_items if i.get("priceVsMin") and i["priceVsMin"] <= 1.0]
        overpriced = [i for i in report.raw_items if i.get("priceVsMin") and i["priceVsMin"] > 1.05]
        overpriced.sort(key=lambda x: x.get("priceSavings") or 0, reverse=True)

        top_overpriced = []
        for i in overpriced[:20]:
            top_overpriced.append({
                "itemId": i.get("id"),
                "productName": i.get("productName") or i.get("name"),
                "price": i.get("price"),
                "minPrice": i.get("minPrice"),
                "minPriceCompetitors": i.get("minPriceCompetitors"),
                "recommendedPrice": i.get("recommendedPrice"),
                "priceSavings": i.get("priceSavings"),
                "shopCount": i.get("shopCount"),
                "category": i.get("category"),
            })

        cheapest.sort(key=lambda x: (x.get("shopCount") or 0), reverse=True)
        top_cheapest = []
        for i in cheapest[:20]:
            top_cheapest.append({
                "itemId": i.get("id"),
                "productName": i.get("productName") or i.get("name"),
                "price": i.get("price"),
                "minPrice": i.get("minPrice"),
                "minPriceCompetitors": i.get("minPriceCompetitors"),
                "priceAdvantage": i.get("priceAdvantage"),
                "shopCount": i.get("shopCount"),
                "category": i.get("category"),
            })

        most_expensive = [i for i in report.raw_items if i.get("priceVsMin") and i["priceVsMin"] > 1.0]
        most_expensive.sort(key=lambda x: (x.get("priceVsMin") or 1.0), reverse=True)
        top_most_expensive = []
        for i in most_expensive[:20]:
            top_most_expensive.append({
                "itemId": i.get("id"),
                "productName": i.get("productName") or i.get("name"),
                "price": i.get("price"),
                "minPrice": i.get("minPrice"),
                "minPriceCompetitors": i.get("minPriceCompetitors"),
                "priceVsMin": i.get("priceVsMin"),
                "priceSavings": i.get("priceSavings"),
                "recommendedPrice": i.get("recommendedPrice"),
                "shopCount": i.get("shopCount"),
                "category": i.get("category"),
            })

        def _demand_score(item):
            sc = float(item.get("shopCount") or 0)
            tp = item.get("topPosition")
            pos_bonus = (100 - float(tp)) if tp is not None and float(tp) > 0 else 0
            return sc * 1000 + pos_bonus

        high_demand = [i for i in report.raw_items if i.get("shopCount") and int(i["shopCount"]) >= 5]
        high_demand.sort(key=_demand_score, reverse=True)
        top_high_demand = []
        for i in high_demand[:20]:
            top_high_demand.append({
                "itemId": i.get("id"),
                "productName": i.get("productName") or i.get("name"),
                "price": i.get("price"),
                "shopCount": i.get("shopCount"),
                "category": i.get("category"),
                "priceVsMin": i.get("priceVsMin"),
                "topPosition": i.get("topPosition"),
                "fromCheapestPosition": i.get("fromCheapestPosition"),
            })

        report.competition_summary = {
            "avgShopCount": avg(all_shop_counts),
            "medianShopCount": sorted(all_shop_counts)[len(all_shop_counts)//2] if all_shop_counts else None,
            "maxShopCount": max(all_shop_counts) if all_shop_counts else None,
            "itemsWithCompetition": len(all_shop_counts),
            "avgPriceVsMin": avg(all_price_ratios),
            "priceBetterThan10pct": sum(1 for r in all_price_ratios if r <= 1.0),
            "priceWorseThan10pct": sum(1 for r in all_price_ratios if r > 1.1),
            "countCheapest": len(cheapest),
            "countOverpriced": len(overpriced),
            "topOverpriced": top_overpriced,
            "topCheapest": top_cheapest,
            "topMostExpensive": top_most_expensive,
            "topHighDemand": top_high_demand,
        }

    # ---------------------------------------------------------------
    # Analýza parametrů dle Zboží.cz spec
    # ---------------------------------------------------------------

    def _analyze_params_spec(self, report: AnalysisReport):
        """Porovná parametry z feedu se specifikací Zboží.cz.
        Pro každou kategorii zjistí:
        - Chybějící parametry (jsou ve spec ale ne ve feedu)
        - Nerozpoznané parametry (jsou ve feedu ale ne ve spec)
        """
        spec = _load_zbozi_params_spec()
        if not spec:
            return

        # Groupujeme feed items dle kategorie
        cat_feed_params: Dict[str, Dict] = defaultdict(lambda: {
            "category": "",
            "categoryPath": "",
            "item_count": 0,
            "spec_params": [],         # parametry ze spec pro tuto kategorii
            "feed_param_names": set(), # unikátní názvy parametrů z feedu
            "missing_critical": [],
            "missing_important": [],
            "missing_supplementary": [],
            "unrecognized": [],
        })

        for item in report.raw_items:
            cat = item.get("category") or "Bez kategorie"
            cat_path = item.get("categoryPath") or cat

            # Najdeme spec pro tuto kategorii (normalizace na lowercase, oddělovač >)
            cat_key = cat_path.replace(" | ", " > ").strip().lower()
            # Zkusíme i jen poslední část (leaf category)
            spec_params = spec.get(cat_key) or []
            if not spec_params:
                # Zkusíme jen název bez cesty
                cat_key_leaf = cat.strip().lower()
                for sk, sv in spec.items():
                    if sk.endswith(cat_key_leaf):
                        spec_params = sv
                        break

            bucket = cat_feed_params[cat]
            bucket["category"] = cat
            bucket["categoryPath"] = cat_path
            bucket["item_count"] += 1
            if spec_params and not bucket["spec_params"]:
                bucket["spec_params"] = spec_params

            # Sbíráme parametry z feedu pro tuto položku
            feed_item = report.feed_items_by_id.get(item.get("id", "")) or {}
            for p in (feed_item.get("params") or []):
                pname = (p.get("name") or "").strip()
                if pname:
                    bucket["feed_param_names"].add(pname)

        # Pro každou kategorii vyhodnotíme chybějící a nerozpoznané
        result = []
        for cat, bucket in cat_feed_params.items():
            spec_params = bucket["spec_params"]
            feed_names_lower = {n.lower() for n in bucket["feed_param_names"]}
            spec_names_lower = {p["name"].lower() for p in spec_params}

            missing_critical = [p["name"] for p in spec_params if p["filterGroup"] == "Kritický" and p["name"].lower() not in feed_names_lower]
            missing_important = [p["name"] for p in spec_params if p["filterGroup"] == "Důležitý" and p["name"].lower() not in feed_names_lower]
            missing_supp = [p["name"] for p in spec_params if p["filterGroup"] == "Doplňkový" and p["name"].lower() not in feed_names_lower]
            unrecognized = [n for n in bucket["feed_param_names"] if n.lower() not in spec_names_lower] if spec_params else []

            result.append({
                "category": bucket["category"],
                "categoryPath": bucket["categoryPath"],
                "itemCount": bucket["item_count"],
                "specFound": bool(spec_params),
                "specParamCount": len(spec_params),
                "feedParamNames": sorted(bucket["feed_param_names"]),
                "missingCritical": missing_critical,
                "missingImportant": missing_important,
                "missingSupplementary": missing_supp,
                "unrecognized": unrecognized,
            })

        result.sort(key=lambda x: (len(x["missingCritical"]), len(x["missingImportant"])), reverse=True)
        report.params_analysis = result

    # ---------------------------------------------------------------
    # Recommendations – Feed & XML
    # ---------------------------------------------------------------

    def _build_feed_recommendations(self, report: AnalysisReport):
        recs: List[Recommendation] = []
        total = report.items_total
        errors = report.items_errors
        improvements = report.items_improvements
        paired = report.items_paired

        if errors > 0:
            pct = round(errors / total * 100) if total else 0
            recs.append(Recommendation(
                priority="critical", section="feed_errors",
                title=f"{errors} položek má chyby ve feedu ({pct} %)",
                detail=(
                    "Položky s chybami se vůbec nezobrazují. Zkontrolujte Centrum prodejce → Diagnostika.\n"
                    "Nejčastější příčiny: chybějící povinný prvek, nedostupný obrázek, nesoulad ceny s webem."
                ),
                example=(
                    "<SHOPITEM>\n"
                    "  <ITEM_ID>SKU-001</ITEM_ID>\n"
                    "  <PRODUCTNAME>Samsung Galaxy S24 128GB Black</PRODUCTNAME>\n"
                    "  <DESCRIPTION>Smartphone s 6,2\" AMOLED displejem...</DESCRIPTION>\n"
                    "  <URL>https://shop.cz/samsung-galaxy-s24</URL>\n"
                    "  <IMGURL>https://shop.cz/img/s24.jpg</IMGURL>\n"
                    "  <PRICE_VAT>18990</PRICE_VAT>\n"
                    "  <CATEGORYTEXT>Elektronika | Mobilní telefony | Samsung</CATEGORYTEXT>\n"
                    "</SHOPITEM>"
                ),
                affected=errors,
            ))

        if total > 0 and paired < total:
            unpaired = total - paired
            ratio = round(paired / total * 100)
            recs.append(Recommendation(
                priority="critical" if ratio < 60 else "important",
                section="pairing",
                title=f"{ratio} % položek spárováno ({unpaired} nespárováno)",
                detail=(
                    "Spárované položky se zobrazují s recenzemi, srovnáním cen a jsou lépe dohledatelné.\n"
                    "Párování ovlivňuje hlavně EAN (nejdůležitější), pak MANUFACTURER a PRODUCTNO."
                ),
                example=(
                    "<EAN>3165140892032</EAN>\n"
                    "<MANUFACTURER>Bosch</MANUFACTURER>\n"
                    "<PRODUCTNO>06019H5200</PRODUCTNO>"
                ),
                affected=unpaired,
            ))

        if report.items_no_delivery > 0:
            pct = round(report.items_no_delivery / total * 100) if total else 0
            recs.append(Recommendation(
                priority="important", section="delivery",
                title=f"{report.items_no_delivery} položek ({pct} %) nemá nastavenou dostupnost (DELIVERY_DATE)",
                detail=(
                    "Produkty bez DELIVERY_DATE se zobrazují hůře – algoritmus upřednostňuje\n"
                    "produkty s jasnou dostupností."
                ),
                example=(
                    "<DELIVERY_DATE>0</DELIVERY_DATE>  <!-- skladem -->\n"
                    "<DELIVERY_DATE>3</DELIVERY_DATE>  <!-- do 3 dní -->"
                ),
                affected=report.items_no_delivery,
            ))

        fq = report.feed_quality
        if fq and fq.get("missing_extra_message", 0) > 0:
            em_recs = fq.get("extra_message_recommendations", [])
            em_detail_parts = []
            for em in em_recs:
                em_detail_parts.append(
                    f"• {em['label']} ({em['message']}): {em['count']} položek – {em['reason']}"
                )
            recs.append(Recommendation(
                priority="important", section="extra_message",
                title=f"Žádná položka nemá EXTRA_MESSAGE – přidejte akční štítky",
                detail=(
                    "EXTRA_MESSAGE se zobrazuje přímo ve výpisu na Nákupech a výrazně zvyšuje CTR.\n"
                    "Doporučené akce pro váš sortiment:\n\n" + "\n".join(em_detail_parts)
                ),
                example=(
                    "<EXTRA_MESSAGE>\n"
                    "  <EXTRA_MESSAGE_TYPE>free_gift</EXTRA_MESSAGE_TYPE>\n"
                    "  <EXTRA_MESSAGE_TEXT>Vzorky parfémů zdarma</EXTRA_MESSAGE_TEXT>\n"
                    "</EXTRA_MESSAGE>"
                ),
                affected=fq["missing_extra_message"],
            ))

        if fq and fq.get("missing_price_before_discount", 0) > 0:
            recs.append(Recommendation(
                priority="important", section="price_discount",
                title="Žádná položka nemá PRICE_BEFORE_DISCOUNT – zviditelněte slevy",
                detail=(
                    "Pokud eshop nabízí zlevněné produkty, přidejte původní cenu.\n"
                    "Na Nákupech se zobrazí přeškrtnutá cena a procentuální sleva."
                ),
                example=(
                    "<PRICE_VAT>1290</PRICE_VAT>\n"
                    "<PRICE_BEFORE_DISCOUNT>1590</PRICE_BEFORE_DISCOUNT>"
                ),
                affected=fq["missing_price_before_discount"],
            ))

        if fq and fq.get("missing_warranty", 0) > 0:
            recs.append(Recommendation(
                priority="tip", section="warranty",
                title="Žádná položka nemá WARRANTY – přidejte záruční dobu",
                detail="Záruční doba zvyšuje důvěryhodnost nabídky.",
                example="<WARRANTY>24</WARRANTY>  <!-- měsíce -->",
                affected=fq["missing_warranty"],
            ))

        if report.items_no_params > 0:
            pct = round(report.items_no_params / total * 100) if total else 0
            top_no_params = sorted(
                [c for c in report.categories_analysis if c.get("noParams", 0) > 0],
                key=lambda x: x["noParams"], reverse=True
            )[:3]
            cat_examples = "\n".join(
                f"  • {c['category']}: {c['noParams']} položek bez parametrů"
                for c in top_no_params
            )
            recs.append(Recommendation(
                priority="important", section="params",
                title=f"{report.items_no_params} položek ({pct} %) nemá žádné parametry (PARAMS)",
                detail=(
                    "Parametry produktu zlepšují filtrování ve výsledcích a párování s katalogem.\n\n"
                    f"Nejpostiženější kategorie:\n{cat_examples}"
                ),
                example=(
                    "<PARAMS>\n"
                    "  <PARAM>\n"
                    "    <PARAM_NAME>Barva</PARAM_NAME>\n"
                    "    <VAL>Černá</VAL>\n"
                    "  </PARAM>\n"
                    "</PARAMS>"
                ),
                affected=report.items_no_params,
            ))

        if report.category_params:
            for cid, attrs in list(report.category_params.items())[:3]:
                attr_names = []
                if isinstance(attrs, list):
                    for a in attrs[:8]:
                        n = a.get("name") or a.get("paramName") or str(a)
                        if n:
                            attr_names.append(n)
                if attr_names:
                    recs.append(Recommendation(
                        priority="tip", section="category_params",
                        title=f"Doporučené parametry pro kategorii ID {cid}",
                        detail="Přidejte tyto parametry dle specifikace: " + ", ".join(attr_names),
                        example="\n".join(
                            f'<PARAM><PARAM_NAME>{n}</PARAM_NAME><VAL>...</VAL></PARAM>'
                            for n in attr_names[:5]
                        ),
                    ))

        if report.items_no_ean > 0:
            pct = round(report.items_no_ean / total * 100) if total else 0
            recs.append(Recommendation(
                priority="important", section="ean",
                title=f"{report.items_no_ean} položek ({pct} %) nemá EAN",
                detail="EAN je nejdůležitější atribut pro párování s produktovým katalogem.",
                example="<EAN>8806095467825</EAN>",
                affected=report.items_no_ean,
            ))

        cs = report.competition_summary
        if cs.get("priceWorseThan10pct", 0) > 0:
            recs.append(Recommendation(
                priority="important", section="pricing",
                title=f"{cs['priceWorseThan10pct']} položek je o více než 10 % dražších než nejlevnější konkurent",
                detail=(
                    f"Průměrný poměr vaší ceny vůči nejlevnějšímu: "
                    f"{cs.get('avgPriceVsMin', '—')}"
                ),
                affected=cs["priceWorseThan10pct"],
            ))

        recs.append(Recommendation(
            priority="important", section="product_names",
            title="Názvy produktů nesmí obsahovat propagační text",
            detail="PRODUCTNAME: bez 'akce', 'výprodej', 'sleva'. Formát: [Značka] [Model] [Klíčová spec].",
            example=(
                "<!-- SPRÁVNĚ -->\n<PRODUCTNAME>Adidas Runfalcon 3.0 W Black EU 39</PRODUCTNAME>\n"
                "<!-- ŠPATNĚ -->\n<!-- <PRODUCTNAME>AKCE! Adidas -30% VÝPRODEJ</PRODUCTNAME> -->"
            ),
        ))

        recs.append(Recommendation(
            priority="important", section="images",
            title="Obrázky: min. 100x100 px, doporučeno 600x600 px+, HTTPS",
            detail="Bílé/průhledné pozadí, bez vodoznaků. IMGURL_ALTERNATIVE pro galerii.",
            example=(
                "<IMGURL>https://shop.cz/img/produkt-600x600.jpg</IMGURL>\n"
                "<IMGURL_ALTERNATIVE>https://shop.cz/img/detail.jpg</IMGURL_ALTERNATIVE>"
            ),
        ))

        if improvements > 0:
            recs.append(Recommendation(
                priority="tip", section="improvements",
                title=f"{improvements} položek lze zlepšit (z diagnostics API)",
                detail="Přidejte chybějící atributy dle Centrum prodejce → Diagnostika → Lze zlepšit.",
                affected=improvements,
            ))

        report.feed_recommendations = recs

    # ---------------------------------------------------------------
    # Recommendations – Sklik Nákupy
    # ---------------------------------------------------------------

    def _build_sklik_recommendations(self, report: AnalysisReport):
        recs: List[Recommendation] = []

        cost = report.perf_cost
        clicks = report.perf_clicks
        convs = report.perf_conversions
        cats = report.categories_analysis

        underbid = sorted(
            [c for c in cats if c.get("cpcGap") and c["cpcGap"] > 0.5 and c.get("clicks", 0) > 0],
            key=lambda x: x["cpcGap"], reverse=True
        )[:5]

        strong_cats = [c for c in cats if c.get("ctr") and c["ctr"] >= 2.0 and c.get("clicks", 0) > 5]
        weak_cats = [c for c in cats if c.get("ctr") and c["ctr"] < 0.5 and c.get("clicks", 0) > 0]

        recs.append(Recommendation(
            priority="important", section="setup",
            title="Správa kampaní Nákupy přes Sklik",
            detail=(
                "1. Sklik.cz → Nová kampaň → Nákupy → vyberte provozovnu\n"
                "2. Geografické cílení: Česká republika\n"
                "3. Vytvořte skupiny produktů dle kategorií\n"
                "4. Nastavte CPC nabídky per skupina\n"
                "5. Využijte automatické strategie (Cílové CPA/ROAS)"
            ),
        ))

        if underbid:
            lines = [f"  • {c['category']}: doporučeno {c.get('avgSuggestedCpc','—')} Kč, nastaveno {c.get('avgMaxCpc','—')} Kč (gap +{c['cpcGap']} Kč)" for c in underbid]
            recs.append(Recommendation(
                priority="critical", section="cpc_gap",
                title=f"V {len(underbid)} kategoriích nabízíte méně CPC než API doporučuje",
                detail="Nízké CPC = ztráta zobrazení v Nákupech. Navyšte nabídky:\n" + "\n".join(lines),
                affected=sum(c.get("items", 0) for c in underbid),
            ))

        if strong_cats:
            lines = [f"  • {c['category']}: CTR {c['ctr']} %, {c['clicks']} kliknutí, {c.get('conversions',0)} konverzí" for c in strong_cats[:5]]
            recs.append(Recommendation(
                priority="important", section="strong_cats",
                title=f"{len(strong_cats)} silných kategorií (CTR >= 2 %) – zvyšte rozpočet",
                detail="Tyto kategorie fungují nejlépe:\n" + "\n".join(lines),
            ))

        if weak_cats:
            lines = [f"  • {c['category']}: CTR {c['ctr']} %, {c['clicks']} kliknutí" for c in weak_cats[:5]]
            recs.append(Recommendation(
                priority="important", section="weak_cats",
                title=f"{len(weak_cats)} slabých kategorií (CTR < 0.5 %) – optimalizujte nebo pozastavte",
                detail="Slabé kategorie:\n" + "\n".join(lines),
                affected=sum(c.get("items", 0) for c in weak_cats),
            ))

        high_comp = [c for c in cats if c.get("avgShopCount") and c["avgShopCount"] > 15 and c.get("clicks", 0) > 0]
        if high_comp:
            lines = [f"  • {c['category']}: průměrně {c['avgShopCount']:.0f} eshopů na kartě" for c in high_comp[:4]]
            recs.append(Recommendation(
                priority="important", section="competition",
                title=f"{len(high_comp)} kategorií s vysokou konkurencí (15+ eshopů na kartě)",
                detail="Vysoká konkurence vyžaduje cenovou konkurenceschopnost a vyšší CPC.\n" + "\n".join(lines),
            ))

        cs = report.competition_summary
        avg_ratio = cs.get("avgPriceVsMin")
        if avg_ratio and avg_ratio > 1.05:
            recs.append(Recommendation(
                priority="important", section="pricing",
                title=f"Průměrně jste o {round((avg_ratio-1)*100, 1)} % dražší než nejlevnější konkurent",
                detail=(
                    f"Průměrný poměr vaší ceny / nejlevnější ceny: {avg_ratio}\n"
                    f"Položky s nejlepší cenou: {cs.get('priceBetterThan10pct', 0)}\n"
                    f"Položky o 10 %+ dražší: {cs.get('priceWorseThan10pct', 0)}"
                ),
            ))

        if cost > 0:
            daily = round(cost / 30, 0)
            recs.append(Recommendation(
                priority="important", section="budget",
                title=f"Denní výdaje: průměr {daily:,.0f} Kč",
                detail=f"Nastavte +30 % rezervu nad průměrem. Sledujte Impression Share.",
            ))

        if convs == 0:
            recs.append(Recommendation(
                priority="critical", section="tracking",
                title="Sledování konverzí není nastaveno – bez toho nelze optimalizovat",
                detail=(
                    "1. Sklik → Nástroje → Konverzní akce → Nová akce\n"
                    "2. Vložte kód na stránku 'Děkujeme za objednávku'\n"
                    "3. Alternativa: import konverzí z Google Analytics / GA4"
                ),
            ))
        elif convs < 30:
            recs.append(Recommendation(
                priority="important", section="tracking",
                title=f"Pouze {convs} konverzí za 30 dní – nedostatek dat pro automatické strategie",
                detail="Pro Cílové ROAS potřebujete min. 30 konverzí/měsíc.",
            ))

        recs.append(Recommendation(
            priority="tip", section="negatives",
            title="Negativní klíčová slova pro Nákupy",
            detail=(
                "Sledujte záložku Vyhledávací dotazy v Skliku a přidávejte:\n"
                "  bazar, použitý, second hand | zdarma, gratis | návod, recenze, test"
            ),
        ))

        report.sklik_recommendations = recs

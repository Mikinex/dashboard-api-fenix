"""
Sklik / Fénix API klient – přepsáno podle OpenAPI spec v1.3.11
Spec: https://api.sklik.cz/v1/openapi.json (staženo 2026-03-05)

Klíčové opravy oproti předchozí verzi:
  - statistics/item a statistics/category vrací HTTP 202 + {"id": N} – jde o
    asynchronní report. Přidány metody get_report() a get_report_content().
  - ReportParams body: klíče jsou "from"/"to" (ne "dateFrom"/"dateTo"),
    "granularity" má default "none" (ne "daily").
  - /nakupy/manufacturers/search má vlastní cestu (ne query param na /manufacturers/).
  - /nakupy/manufacturers/by-ids je separátní endpoint.
  - offset v shop-items je integer (ne opaque cursor string).
  - Přidány metody get_current_user(), get_credit(), get_shops(),
    get_report(), get_report_content().
  - get_accessible_users() v spec NEEXISTUJE – metoda vrací NotImplementedError.
"""

import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List
import requests


class RateLimiter:
    """Zajišťuje minimální interval mezi HTTP požadavky (rate limit: 5 req/s)."""

    def __init__(self, min_interval_seconds: float):
        self.min_interval = min_interval_seconds
        self.last_call = 0.0

    def wait(self):
        elapsed = time.time() - self.last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self.last_call = time.time()


class SklikAPIError(Exception):
    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class SklikAPI:
    BASE_URL = "https://api.sklik.cz/v1"

    def __init__(self, user_token: str, premise_id: str = None, user_id: str = None):
        # user_token je Fénix refresh token (JWT získaný přes sklik.cz)
        self.user_token = user_token
        self.refresh_token = user_token  # zpětná kompatibilita
        self.premise_id = str(premise_id) if premise_id else None
        self.user_id = str(user_id) if user_id else None
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        # Access token cache
        self._access_token: Optional[str] = None
        self._token_expires: float = 0.0
        # Rate limit: 5 req/s dle spec → interval 200 ms
        self._rl = RateLimiter(0.2)

    # -------------------------------------------------------------------------
    # Autentizace
    # -------------------------------------------------------------------------

    def _authenticate(self):
        """
        POST /user/token – výměna refresh tokenu za access token.

        Podle spec (Body_get_access_token_user_token_post):
          - grant_type: "client_credentials" (povinné)
          - user_id: integer | null (volitelné) – ID cizího účtu, ke kterému
            máme oprávnění. Musí být v request body, NE v query stringu.
        Auth: Bearer {refresh_token} v hlavičce.
        """
        if self._access_token and time.time() < self._token_expires:
            return  # Token je stále platný

        url = f"{self.BASE_URL}/user/token"
        form_data: Dict[str, Any] = {"grant_type": "client_credentials"}
        # user_id jde do body (ne do query) pro přístup k cizímu účtu
        if self.user_id:
            form_data["user_id"] = self.user_id

        try:
            resp = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.user_token}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data=form_data,
                timeout=30,
            )
        except requests.RequestException as e:
            raise SklikAPIError(f"Chyba autentizace: {e}")

        if resp.status_code == 401:
            raise SklikAPIError("Neplatný API klíč (refresh token)", 401)
        if resp.status_code == 403:
            raise SklikAPIError(
                "Přístup zakázán – zkontrolujte oprávnění API klíče nebo user_id", 403
            )
        if not resp.ok:
            raise SklikAPIError(
                f"Chyba autentizace: HTTP {resp.status_code}: {resp.text[:300]}",
                resp.status_code,
            )

        try:
            data = resp.json()
        except Exception:
            raise SklikAPIError("Neplatná JSON odpověď při autentizaci")

        # Spec: access_token je v odpovědi pod klíčem "access_token" nebo "token"
        self._access_token = data.get("access_token") or data.get("token")
        expires_in = data.get("expires_in", 3600)
        # Refresh 5 minut před vypršením
        self._token_expires = time.time() + expires_in - 300

        if not self._access_token:
            # Fallback: Fénix může vracet refresh token přímo jako access token
            self._access_token = self.user_token
            self._token_expires = time.time() + 3300

        self.session.headers["Authorization"] = f"Bearer {self._access_token}"

    def _ensure_auth(self):
        self._authenticate()

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _params(self, extra: Dict = None) -> Dict:
        """Sestaví základní query parametry. premiseId je povinný pro většinu endpointů."""
        p: Dict[str, Any] = {}
        if self.premise_id:
            p["premiseId"] = int(self.premise_id)
        # userId se NEPOSÍLÁ do query – jde pouze do body při /user/token
        if extra:
            p.update(extra)
        return p

    def _get(self, endpoint: str, params: Dict = None) -> Any:
        self._ensure_auth()
        self._rl.wait()
        url = f"{self.BASE_URL}{endpoint}"
        try:
            resp = self.session.get(url, params=self._params(params), timeout=30)
        except requests.RequestException as e:
            raise SklikAPIError(f"Síťová chyba: {e}")
        return self._handle_response(resp, endpoint)

    def _post(self, endpoint: str, params: Dict = None, json_body: Any = None) -> Any:
        self._ensure_auth()
        self._rl.wait()
        url = f"{self.BASE_URL}{endpoint}"
        try:
            resp = self.session.post(
                url, params=self._params(params), json=json_body, timeout=30
            )
        except requests.RequestException as e:
            raise SklikAPIError(f"Síťová chyba: {e}")
        return self._handle_response(resp, endpoint)

    def _handle_response(self, resp: requests.Response, endpoint: str) -> Any:
        if resp.status_code == 401:
            # Token vypršel – invalidujeme cache, příští volání provede re-auth
            self._access_token = None
            raise SklikAPIError("Token vypršel, zkuste znovu", 401)
        if resp.status_code == 403:
            raise SklikAPIError(
                f"Přístup zakázán k {endpoint} – zkontrolujte scope API klíče a premiseId",
                403,
            )
        if resp.status_code == 404:
            raise SklikAPIError(f"Endpoint nenalezen: {endpoint}", 404)
        if resp.status_code == 422:
            # Validační chyba – vrátíme detail z JSON
            try:
                detail = resp.json().get("detail", resp.text[:300])
            except Exception:
                detail = resp.text[:300]
            raise SklikAPIError(f"Validační chyba {endpoint}: {detail}", 422)
        if resp.status_code == 429:
            raise SklikAPIError("Rate limit – zkuste znovu za chvíli", 429)
        if resp.status_code >= 500:
            raise SklikAPIError(
                f"Chyba serveru Sklik API ({resp.status_code})", resp.status_code
            )
        # HTTP 202 Accepted (asynchronní report – vrátíme JSON s id reportu)
        if not resp.ok and resp.status_code != 202:
            raise SklikAPIError(
                f"HTTP {resp.status_code}: {resp.text[:300]}", resp.status_code
            )
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text}

    # -------------------------------------------------------------------------
    # User endpointy
    # -------------------------------------------------------------------------

    def get_current_user(self) -> Any:
        """
        GET /user/me – informace o právě přihlášeném uživateli.
        Odpověď: {userId, userName, actor, scope}
        """
        return self._get("/user/me")

    def get_credit(self) -> Any:
        """GET /user/me/credit – informace o kreditu účtu."""
        return self._get("/user/me/credit")

    def get_accessible_users(self) -> Any:
        """
        Endpoint pro výpis přístupných (cizích) účtů v OpenAPI spec v1.3.11
        NEEXISTUJE. Použijte /user/me pro identifikaci aktuálního účtu.
        Pro přístup k cizímu účtu předejte user_id do konstruktoru.
        """
        raise NotImplementedError(
            "Endpoint get_accessible_users není v Sklik API spec v1.3.11. "
            "Pro přístup k cizímu účtu použijte parametr user_id v konstruktoru SklikAPI."
        )

    # -------------------------------------------------------------------------
    # Shops
    # -------------------------------------------------------------------------

    def get_shops(self, shop_ids: List[int] = None) -> Any:
        """
        GET /nakupy/shops/ – detaily obchodů.
        Param id (povinný): seznam shop ID (max 100). premiseId je také povinný.
        """
        if not shop_ids and self.premise_id:
            shop_ids = [int(self.premise_id)]
        params: Dict[str, Any] = {}
        if shop_ids:
            # API spec: typ array → posíláme víckrát stejný klíč
            params["id"] = [int(i) for i in shop_ids[:100]]
        return self._get("/nakupy/shops/", params)

    # -------------------------------------------------------------------------
    # Diagnostika
    # -------------------------------------------------------------------------

    def get_diagnostics(self) -> Any:
        """GET /nakupy/diagnostics/item – diagnostická data nabídek obchodu."""
        return self._get("/nakupy/diagnostics/item")

    # -------------------------------------------------------------------------
    # Položky (shop-items)
    # -------------------------------------------------------------------------

    def get_items(
        self,
        limit: int = 30,
        offset: int = None,
        load_product_detail: bool = True,
        load_search_info: bool = True,
    ) -> Any:
        """
        GET /nakupy/shop-items/ – seznam položek obchodu.

        Limity dle spec:
          - s loadProductDetail: max 30 položek/request
          - s loadSearchInfo: max 300 položek/request
          - bez obojího: max 3000 položek/request

        Offset je integer (NE opaque cursor string).
        """
        params: Dict[str, Any] = {}
        if offset is not None:
            params["offset"] = int(offset)
        if load_product_detail:
            params["loadProductDetail"] = True
            params["limit"] = min(limit, 30)
        elif load_search_info:
            params["loadSearchInfo"] = True
            params["limit"] = min(limit, 300)
        else:
            params["limit"] = min(limit, 3000)
        if load_search_info and load_product_detail:
            params["loadSearchInfo"] = True
        return self._get("/nakupy/shop-items/", params)

    def get_items_basic(self, limit: int = 3000, offset: int = None) -> Any:
        """Položky bez detailů – max 3000/request."""
        params: Dict[str, Any] = {"limit": min(limit, 3000)}
        if offset is not None:
            params["offset"] = int(offset)
        return self._get("/nakupy/shop-items/", params)

    # -------------------------------------------------------------------------
    # Feedy
    # -------------------------------------------------------------------------

    def get_feeds(self) -> Any:
        """GET /nakupy/feeds/ – seznam feedů obchodu."""
        return self._get("/nakupy/feeds/")

    # -------------------------------------------------------------------------
    # Kampaně
    # -------------------------------------------------------------------------

    def get_campaigns(self) -> Any:
        """GET /nakupy/campaigns/ – seznam kampaní."""
        return self._get("/nakupy/campaigns/")

    # -------------------------------------------------------------------------
    # Statistiky – ASYNCHRONNÍ FLOW
    #
    # Spec: POST /nakupy/statistics/item vrací HTTP 202 {"id": N, "meta": {...}}
    # Report se generuje asynchronně. Výsledek stáhneme přes:
    #   GET /sklik/reports/        – seznam reportů (zjistíme stav)
    #   GET /sklik/reports/{id}    – obsah hotového reportu
    #
    # ReportParams:
    #   "from": ISO8601 datetime (POVINNÉ) – POZOR: klíč je "from", ne "dateFrom"!
    #   "to":   ISO8601 datetime (POVINNÉ) – POZOR: klíč je "to", ne "dateTo"!
    #   "granularity": "daily"|"weekly"|"monthly"|"quarterly"|"yearly"|"none"
    #                  (default "none" dle spec)
    #   "format": list – "json","csv","tsv","xml","html","xlsx","jsonWebview"
    # -------------------------------------------------------------------------

    def _iso(self, dt: datetime) -> str:
        """Formátuje datetime do ISO 8601 s Z suffix (UTC)."""
        if dt.tzinfo is None:
            # Předpokládáme lokální čas, převedeme na UTC string
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _request_and_wait_stats(
        self, endpoint: str, days: int, granularity: str, max_wait: int = 120
    ) -> Any:
        """
        Interní helper: odešle POST na statistics endpoint, počká na dokončení
        asynchronního reportu a vrátí jeho obsah.

        Sklik statistics API je ASYNCHRONNÍ:
          1. POST /nakupy/statistics/item → HTTP 202 {"id": N, "meta": {...}}
          2. Čekáme, až GET /sklik/reports/ ukáže status "done" pro dané id
          3. GET /sklik/reports/{N} → {"stats": [...], "sums": {...}}

        Pro kompatibilitu s analyzer.py přeformátujeme výsledek do:
          {"data": [...], "totalCount": N}
        kde každý záznam z "stats" jde do "data".
        """
        dt_from = datetime.now() - timedelta(days=days)
        dt_to = datetime.now()
        body = {
            "from": self._iso(dt_from),
            "to": self._iso(dt_to),
            "granularity": granularity,
            "format": ["json"],
        }
        resp = self._post(endpoint, json_body=body)

        # HTTP 202: asynchronní report – čekáme na dokončení
        report_id = None
        if isinstance(resp, dict):
            report_id = resp.get("id")

        if not report_id:
            # Synchronní odpověď nebo chyba – vrátíme jak je
            return resp

        # Čekáme na report
        content = self.wait_for_report(report_id, max_wait_seconds=max_wait)
        if not content:
            # Timeout – vrátíme prázdná data s info o report_id
            return {"data": [], "totalCount": 0, "_debug": f"timeout report_id={report_id}"}

        # Přeformátujeme na {"data": [...], "totalCount": N} pro kompatibilitu s analyzer.py
        # Report může mít klíč "stats", "items" nebo "data"
        if isinstance(content, dict):
            stats = content.get("stats") or content.get("items") or content.get("data") or []
        else:
            stats = []
        return {"data": stats, "totalCount": len(stats), "_raw_keys": list(content.keys()) if isinstance(content, dict) else []}

    def get_stats_item(self, days: int = 30, granularity: str = "daily") -> Any:
        """
        POST /nakupy/statistics/item – statistiky položek.

        API je ASYNCHRONNÍ: tato metoda automaticky počká na dokončení reportu
        (max 120 s) a vrátí výsledek ve formátu {"data": [...], "totalCount": N}
        kompatibilním s analyzer.py.

        OPRAVA: body klíče jsou "from"/"to" (ne "dateFrom"/"dateTo").
        """
        return self._request_and_wait_stats(
            "/nakupy/statistics/item", days, granularity
        )

    def get_stats_category(self, days: int = 30, granularity: str = "daily") -> Any:
        """
        POST /nakupy/statistics/category – statistiky kategorií.

        API je ASYNCHRONNÍ: tato metoda automaticky počká na dokončení reportu
        (max 120 s) a vrátí výsledek ve formátu {"data": [...], "totalCount": N}.

        OPRAVA: body klíče jsou "from"/"to" (ne "dateFrom"/"dateTo").
        """
        return self._request_and_wait_stats(
            "/nakupy/statistics/category", days, granularity
        )

    # -------------------------------------------------------------------------
    # Sklik reporty (stahování asynchronních výsledků)
    # -------------------------------------------------------------------------

    def get_reports(self, limit: int = 20, offset: int = 0) -> Any:
        """
        GET /sklik/reports/ – seznam reportů uživatele.
        Odpověď: {items: [{id, reportType, name, status, startDate, endDate, ...}]}
        Pozor: tento endpoint NEVYŽADUJE premiseId – voláme bez něj.
        """
        # Spec: /sklik/reports/ nemá premiseId v parametrech → nesmíme posílat
        self._ensure_auth()
        self._rl.wait()
        url = f"{self.BASE_URL}/sklik/reports/"
        try:
            resp = self.session.get(
                url,
                params={"limit": limit, "offset": offset},
                timeout=30,
            )
        except requests.RequestException as e:
            raise SklikAPIError(f"Síťová chyba: {e}")
        return self._handle_response(resp, "/sklik/reports/")

    def get_report_content(self, report_id: int, format: str = "json") -> Any:
        """
        GET /sklik/reports/{reportId} – obsah hotového reportu.

        Parametry:
          report_id: ID reportu z odpovědi statistik (HTTP 202 -> id)
          format: "json"|"csv"|"tsv"|"xml"|"html"|"xlsx"|"jsonWebview"

        Odpověď: {stats: [...], sums: {...}}
        Pozor: tento endpoint NEVYŽADUJE premiseId.
        """
        self._ensure_auth()
        self._rl.wait()
        url = f"{self.BASE_URL}/sklik/reports/{int(report_id)}"
        try:
            # format parametr způsobuje 400 – API vrací JSON bez něj
            resp = self.session.get(url, timeout=60)
        except requests.RequestException as e:
            raise SklikAPIError(f"Síťová chyba: {e}")
        return self._handle_response(resp, f"/sklik/reports/{report_id}")

    def wait_for_report(self, report_id: int, max_wait_seconds: int = 120) -> Any:
        """
        Čeká, až bude asynchronní report připraven, pak vrátí jeho obsah.

        Kontroluje stav přes GET /sklik/reports/ každé 2 sekundy.
        Vrací None pokud report není hotový do max_wait_seconds.
        """
        deadline = time.time() + max_wait_seconds
        while time.time() < deadline:
            try:
                reports = self.get_reports(limit=50)
                items = reports.get("items", []) if isinstance(reports, dict) else []
                for item in items:
                    if item.get("id") == report_id:
                        status = item.get("status", "")
                        if status in ("done", "finished", "completed", "ready", "ok"):
                            return self.get_report_content(report_id)
                        elif status in ("error", "failed", "cancelled"):
                            raise SklikAPIError(
                                f"Report {report_id} selhal: {status}"
                            )
                        # Jinak čekáme (status: "pending", "processing", ...)
                # Zkusíme obsah reportu přímo (bez ohledu na status)
                try:
                    content = self.get_report_content(report_id)
                    if content and (content.get("stats") or content.get("data") or content.get("items")):
                        return content
                except SklikAPIError:
                    pass
            except SklikAPIError:
                raise
            time.sleep(2)
        return None  # Timeout – report ještě není hotový

    # -------------------------------------------------------------------------
    # Recenze
    # -------------------------------------------------------------------------

    def get_reviews(self, limit: int = 100, days: int = 30) -> Any:
        """
        GET /nakupy/reviews/ – recenze obchodu.
        Max 180 dní zpět (limit Zboží.cz API).
        """
        dt_from = datetime.now() - timedelta(days=min(days, 180))
        return self._get(
            "/nakupy/reviews/",
            {
                "fromDatetime": self._iso(dt_from),
                "limit": limit,
            },
        )

    def get_product_reviews(self, limit: int = 100, days: int = 30) -> Any:
        """
        GET /nakupy/product-reviews/ – recenze produktů.
        Spec: max 1000 limit, default 500. Max 180 dní zpět.
        """
        dt_from = datetime.now() - timedelta(days=min(days, 180))
        return self._get(
            "/nakupy/product-reviews/",
            {
                "fromDatetime": self._iso(dt_from),
                "limit": min(limit, 1000),
            },
        )

    # -------------------------------------------------------------------------
    # Produkty (konkurenční data)
    # -------------------------------------------------------------------------

    def get_products(self, product_ids: List) -> Any:
        """
        GET /nakupy/products/ – konkurenční data produktů.
        Param productId: array (povinné), max 10 ID najednou.
        """
        if not product_ids:
            raise SklikAPIError("product_ids nesmí být prázdný seznam")
        # Spec: typ array – posíláme jako list (requests je zserializuje jako ?productId=X&productId=Y)
        params: Dict[str, Any] = {
            "productId": [str(i) for i in product_ids[:10]]
        }
        return self._get("/nakupy/products/", params)

    # -------------------------------------------------------------------------
    # Kategorie
    # -------------------------------------------------------------------------

    def get_categories(self, category_ids: List) -> Any:
        """
        GET /nakupy/categories/ – detaily kategorií.
        Param categoryId: array (povinné), max 10 ID najednou.
        """
        params: Dict[str, Any] = {
            "categoryId": [int(i) for i in category_ids[:10]]
        }
        return self._get("/nakupy/categories/", params)

    def get_categories_tree(self) -> Any:
        """GET /nakupy/categories/tree – strom kategorií."""
        return self._get("/nakupy/categories/tree")

    # -------------------------------------------------------------------------
    # Výrobci
    # -------------------------------------------------------------------------

    def get_manufacturers(self) -> Any:
        """GET /nakupy/manufacturers/ – seznam všech výrobců obchodu."""
        return self._get("/nakupy/manufacturers/")

    def get_manufacturers_by_ids(self, manufacturer_ids: List[int]) -> Any:
        """
        GET /nakupy/manufacturers/by-ids – výrobci podle ID.
        OPRAVA: V předchozí verzi chyběl tento endpoint (existuje v spec).
        """
        return self._get(
            "/nakupy/manufacturers/by-ids",
            {"id": [int(i) for i in manufacturer_ids]},
        )

    def get_manufacturers_search(self, query: str) -> Any:
        """
        GET /nakupy/manufacturers/search – vyhledání výrobce podle jména.
        OPRAVA: Správná cesta je /nakupy/manufacturers/search (ne /manufacturers/ s param query).
        Povinný param: name (ne query).
        """
        return self._get("/nakupy/manufacturers/search", {"name": query})

    # -------------------------------------------------------------------------
    # Feed – stažení a parsování XML
    # -------------------------------------------------------------------------

    def download_feed(self, feed_url: str, timeout: int = 120) -> List[Dict]:
        """Stáhne XML feed a vrátí seznam položek s klíčovými elementy."""
        if not feed_url:
            raise SklikAPIError("Feed URL je prázdné")
        try:
            resp = requests.get(feed_url, timeout=timeout)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise SklikAPIError(f"Nelze stáhnout feed: {e}")

        items = []
        try:
            root = ET.fromstring(resp.content)
            for elem in root.iter():
                local = self._local_tag(elem.tag)
                if local == "shopitem":
                    item = self._parse_shopitem(elem)
                    if item:
                        items.append(item)
        except ET.ParseError as e:
            raise SklikAPIError(f"Chyba parsování XML feedu: {e}")
        return items

    @staticmethod
    def _local_tag(tag: str) -> str:
        """Odstraní XML namespace prefix z názvu tagu."""
        if not tag:
            return ""
        if "}" in tag:
            tag = tag.split("}", 1)[1]
        return tag.lower()

    @classmethod
    def _parse_shopitem(cls, elem) -> Optional[Dict]:
        """
        Parsuje element <SHOPITEM> z XML feedu.
        Parametry jsou uvnitř <PARAMS><PARAM>...</PARAM></PARAMS>.
        """
        children: Dict[str, Optional[str]] = {}
        params_elems = []

        for child in elem:
            local = cls._local_tag(child.tag)
            if local == "params":
                # Zboží.cz feed: <PARAMS><PARAM>...</PARAM></PARAMS>
                for sub in child:
                    if cls._local_tag(sub.tag) == "param":
                        params_elems.append(sub)
            elif local == "param":
                # Fallback: <PARAM> jako přímý potomek (nestandardní feedy)
                params_elems.append(child)
            elif local not in children:
                children[local] = (child.text or "").strip() if child.text else None

        item_id = children.get("item_id")
        if not item_id:
            return None

        params = []
        for pe in params_elems:
            pname = None
            pval = None
            for sub in pe:
                sl = cls._local_tag(sub.tag)
                if sl == "param_name":
                    pname = (sub.text or "").strip()
                elif sl == "val":
                    pval = (sub.text or "").strip()
            if pname:
                params.append({"name": pname, "value": pval or ""})

        price_str = children.get("price_vat")
        price = None
        if price_str:
            try:
                price = float(price_str.replace(",", ".").replace(" ", ""))
            except ValueError:
                pass

        return {
            "itemId": item_id,
            "price": price,
            "deliveryDate": children.get("delivery_date"),
            "ean": children.get("ean"),
            "url": children.get("url"),
            "imgUrl": children.get("imgurl"),
            "productName": children.get("productname"),
            "manufacturer": children.get("manufacturer"),
            "categoryText": children.get("categorytext"),
            "params": params,
        }

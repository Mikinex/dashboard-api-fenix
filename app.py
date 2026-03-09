import csv
import dataclasses
import hashlib
import io
import json
import math
import os
import queue
import time
import traceback

from flask import Flask, render_template, request, jsonify, Response

import anthropic

from sklik_api import SklikAPI, SklikAPIError
from analyzer import SklikAnalyzer
from history import save_snapshot, get_snapshots, get_comparison, get_price_history, get_price_movers

# Načti .env pokud existuje
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)
app.config["JSON_ENSURE_ASCII"] = False

# ---- Jednoduchý in-memory cache (TTL = 10 minut) ----
_cache: dict = {}  # key -> (timestamp, data)
CACHE_TTL = 600  # sekundy


def _cache_key(api_key: str, premise_id: str, user_id: str = None) -> str:
    raw = f"{api_key}:{premise_id}:{user_id or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _cache_get(key: str):
    entry = _cache.get(key)
    if entry and (time.time() - entry[0]) < CACHE_TTL:
        return entry[1]
    return None


def _cache_set(key: str, data):
    _cache[key] = (time.time(), data)

# Výchozí token z prostředí (pokud je nastaven v .env)
DEFAULT_API_TOKEN = os.environ.get("API_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


def _safe_value(v):
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def _to_dict(obj):
    if dataclasses.is_dataclass(obj):
        return {k: _to_dict(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, list):
        return [_to_dict(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items()}
    if isinstance(obj, float):
        return _safe_value(obj)
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/users", methods=["POST"])
def get_users():
    """Vrátí seznam dostupných klientů (user ID) pro daný token."""
    body = request.get_json(silent=True) or {}
    api_key = (body.get("api_key") or DEFAULT_API_TOKEN).strip()
    if not api_key:
        return jsonify({"error": "Zadejte API token."}), 400
    try:
        api = SklikAPI(api_key)
        data = api._get("/user/accessible_users")
        return app.response_class(
            response=json.dumps(data, ensure_ascii=False),
            mimetype="application/json",
        )
    except SklikAPIError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": traceback.format_exc()}), 500


@app.route("/analyze", methods=["POST"])
def analyze():
    body = request.get_json(silent=True) or {}
    api_key = (body.get("api_key") or DEFAULT_API_TOKEN).strip()
    premise_id = (body.get("premise_id") or "").strip()
    user_id = (body.get("user_id") or "").strip() or None
    force_refresh = body.get("force_refresh", False)

    if not api_key:
        return jsonify({"error": "Zadejte API klíč."}), 400

    # Check cache
    ck = _cache_key(api_key, premise_id, user_id)
    if not force_refresh:
        cached = _cache_get(ck)
        if cached:
            return app.response_class(
                response=json.dumps(cached, ensure_ascii=False),
                mimetype="application/json",
            )

    try:
        api = SklikAPI(api_key, premise_id, user_id=user_id)
        analyzer = SklikAnalyzer(api)
        report = analyzer.analyze(premise_id)

        diag_status = report.endpoint_status.get("diagnostics", "")
        if "401" in diag_status or "Neplatné" in diag_status or "Neplatný" in diag_status:
            return jsonify({"error": f"Neplatné přihlašovací údaje: {diag_status}"}), 401
        if "403" in diag_status:
            return jsonify({"error": f"Přístup zakázán: {diag_status}. Zkontrolujte scope API klíče a ID provozovny."}), 403

        report.feed_items_by_id = {}
        result = _to_dict(report)
        _cache_set(ck, result)

        # Auto-save snapshot do historie
        try:
            save_snapshot(premise_id, result)
        except Exception:
            app.logger.warning("Nepodařilo se uložit historický snímek: %s", traceback.format_exc())

        return app.response_class(
            response=json.dumps(result, ensure_ascii=False),
            mimetype="application/json",
        )
    except SklikAPIError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        tb = traceback.format_exc()
        app.logger.error(tb)
        return jsonify({
            "error": "Neočekávaná chyba serveru.",
            "trace": tb,
        }), 500


@app.route("/analyze/stream", methods=["POST"])
def analyze_stream():
    """SSE endpoint – sends progress events and final result."""
    body = request.get_json(silent=True) or {}
    api_key = (body.get("api_key") or "").strip()
    premise_id = (body.get("premise_id") or "").strip()
    user_id = (body.get("user_id") or "").strip() or None

    if not api_key:
        return jsonify({"error": "Zadejte API klíč (refresh token)."}), 400

    q = queue.Queue()

    def progress_cb(pct, msg):
        q.put(("progress", pct, msg))

    import threading

    result_q = queue.Queue()

    def run_analysis():
        try:
            api = SklikAPI(api_key, premise_id, user_id=user_id)
            analyzer = SklikAnalyzer(api, progress_cb=progress_cb)
            report = analyzer.analyze(premise_id)
            report.feed_items_by_id = {}
            result_dict = _to_dict(report)
            try:
                save_snapshot(premise_id, result_dict)
            except Exception:
                pass
            result_q.put(("ok", result_dict))
        except SklikAPIError as e:
            result_q.put(("api_error", str(e)))
        except Exception:
            result_q.put(("error", traceback.format_exc()))

    def stream():
        t = threading.Thread(target=run_analysis, daemon=True)
        t.start()
        while True:
            try:
                kind, pct, msg = q.get(timeout=0.5)
                yield f"data: {json.dumps({'type': 'progress', 'pct': pct, 'msg': msg}, ensure_ascii=False)}\n\n"
            except (queue.Empty, ValueError):
                pass
            try:
                result = result_q.get_nowait()
                while not q.empty():
                    try:
                        kind, pct, msg = q.get_nowait()
                        yield f"data: {json.dumps({'type': 'progress', 'pct': pct, 'msg': msg}, ensure_ascii=False)}\n\n"
                    except (queue.Empty, ValueError):
                        break
                if result[0] == "ok":
                    yield f"data: {json.dumps({'type': 'result', 'data': result[1]}, ensure_ascii=False)}\n\n"
                elif result[0] == "api_error":
                    yield f"data: {json.dumps({'type': 'error', 'error': result[1]}, ensure_ascii=False)}\n\n"
                else:
                    yield f"data: {json.dumps({'type': 'error', 'error': 'Neočekávaná chyba.', 'trace': result[1]}, ensure_ascii=False)}\n\n"
                break
            except queue.Empty:
                pass

    return Response(stream(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@app.route("/api/call", methods=["POST"])
def api_call():
    body = request.get_json(silent=True) or {}
    api_key = (body.get("api_key") or "").strip()
    premise_id = (body.get("premise_id") or "").strip()
    user_id = (body.get("user_id") or "").strip() or None
    endpoint = (body.get("endpoint") or "").strip()
    params = body.get("params") or {}

    if not api_key:
        return jsonify({"error": "Zadejte API klíč (refresh token)."}), 400
    if not endpoint:
        return jsonify({"error": "Zadejte endpoint."}), 400

    try:
        api = SklikAPI(api_key, premise_id, user_id=user_id)

        endpoint_map = {
            "diagnostics": lambda: api.get_diagnostics(),
            "items": lambda: api.get_items(
                limit=int(params.get("limit", 30)),
                offset=params.get("offset"),
                load_product_detail=bool(params.get("loadProductDetail", True)),
                load_search_info=bool(params.get("loadSearchInfo", True)),
            ),
            "items_basic": lambda: api.get_items_basic(
                limit=int(params.get("limit", 100)),
                offset=params.get("offset"),
            ),
            "feeds": lambda: api.get_feeds(),
            "feed_download": lambda: api.download_feed(
                params.get("feed_url", ""),
            ),
            "campaigns": lambda: api.get_campaigns(),
            "stats_item": lambda: api.get_stats_item(
                days=int(params.get("days", 30)),
            ),
            "stats_category": lambda: api.get_stats_category(
                days=int(params.get("days", 30)),
            ),
            "reviews": lambda: api.get_reviews(
                limit=int(params.get("limit", 100)),
                days=int(params.get("days", 30)),
            ),
            "product_reviews": lambda: api.get_product_reviews(
                limit=int(params.get("limit", 100)),
                days=int(params.get("days", 30)),
            ),
            "products": lambda: api.get_products(
                params.get("product_ids", []),
            ),
            "categories": lambda: api.get_categories(
                params.get("category_ids", []),
            ),
            "categories_tree": lambda: api.get_categories_tree(),
            "manufacturers": lambda: api.get_manufacturers(),
        }

        if endpoint not in endpoint_map:
            return jsonify({"error": f"Neznámý endpoint: {endpoint}"}), 400

        result = endpoint_map[endpoint]()
        return app.response_class(
            response=json.dumps(result, ensure_ascii=False, default=str),
            mimetype="application/json",
        )
    except SklikAPIError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        tb = traceback.format_exc()
        app.logger.error(tb)
        return jsonify({"error": "Chyba při volání API.", "trace": tb}), 500


@app.route("/history/snapshots", methods=["POST"])
def history_snapshots():
    """Vrátí historické snímky pro provozovnu."""
    body = request.get_json(silent=True) or {}
    premise_id = (body.get("premise_id") or "").strip()
    if not premise_id:
        return jsonify({"error": "Chybí premise_id."}), 400
    snapshots = get_snapshots(premise_id, limit=body.get("limit", 30))
    return app.response_class(
        response=json.dumps(snapshots, ensure_ascii=False, default=str),
        mimetype="application/json",
    )


@app.route("/history/comparison", methods=["POST"])
def history_comparison():
    """Porovná poslední dva snímky."""
    body = request.get_json(silent=True) or {}
    premise_id = (body.get("premise_id") or "").strip()
    if not premise_id:
        return jsonify({"error": "Chybí premise_id."}), 400
    comparison = get_comparison(premise_id)
    if not comparison:
        return jsonify({"error": "Nedostatek dat pro porovnání (potřeba min. 2 analýzy)."}), 404
    return app.response_class(
        response=json.dumps(comparison, ensure_ascii=False, default=str),
        mimetype="application/json",
    )


@app.route("/history/price", methods=["POST"])
def history_price():
    """Vrátí cenovou historii položky."""
    body = request.get_json(silent=True) or {}
    premise_id = (body.get("premise_id") or "").strip()
    item_id = (body.get("item_id") or "").strip()
    if not premise_id or not item_id:
        return jsonify({"error": "Chybí premise_id nebo item_id."}), 400
    history = get_price_history(premise_id, item_id)
    return app.response_class(
        response=json.dumps(history, ensure_ascii=False, default=str),
        mimetype="application/json",
    )


@app.route("/history/price-movers", methods=["POST"])
def history_price_movers():
    """Vrátí položky s největší změnou cen konkurence."""
    body = request.get_json(silent=True) or {}
    premise_id = (body.get("premise_id") or "").strip()
    if not premise_id:
        return jsonify({"error": "Chybí premise_id."}), 400
    movers = get_price_movers(premise_id)
    return app.response_class(
        response=json.dumps(movers, ensure_ascii=False, default=str),
        mimetype="application/json",
    )


@app.route("/ai/recommend", methods=["POST"])
def ai_recommend():
    """SSE endpoint – streamuje AI doporučení z Claude."""
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY není nastaven v .env"}), 500

    body = request.get_json(silent=True) or {}
    report_data = body.get("report")
    if not report_data:
        return jsonify({"error": "Chybí data analýzy."}), 400

    # Připravíme shrnutí pro Claude (jen agregovaná data, žádné klíče)
    summary = _build_ai_summary(report_data)

    def stream():
        try:
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            with client.messages.stream(
                model="claude-sonnet-4-5",
                max_tokens=4096,
                system="""Jsi expert na e-commerce a online marketing, specializuješ se na Zboží.cz a Sklik Nákupy.
Uživatel ti posílá data z analýzy svého e-shopu na Zboží.cz. Tvým úkolem je:

1. **Shrnutí stavu** – stručně zhodnoť celkový stav e-shopu (2-3 věty)
2. **Top 3 priority** – co by měl řešit HNED (konkrétní akce, ne obecné rady)
3. **Cenová strategie** – zhodnoť cenovou konkurenceschopnost a doporuč konkrétní kroky
4. **Feed kvalita** – co zlepšit ve feedu (parametry, EAN, dostupnost)
5. **Výkonnost kampaní** – zhodnoť PNO, CTR, CPC a doporuč optimalizace
6. **Skryté příležitosti** – co většina eshopů přehlíží

Piš česky, stručně, konkrétně. Používej čísla z dat. Formátuj jako Markdown s nadpisy ##.""",
                messages=[{"role": "user", "content": summary}],
            ) as stream_response:
                for text in stream_response.text_stream:
                    yield f"data: {json.dumps({'type': 'token', 'text': text}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
        except anthropic.AuthenticationError:
            yield f"data: {json.dumps({'type': 'error', 'error': 'Neplatný ANTHROPIC_API_KEY. Zkontrolujte .env soubor.'}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)}, ensure_ascii=False)}\n\n"

    return Response(stream(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


def _build_ai_summary(r):
    """Sestaví textové shrnutí analýzy pro Claude."""
    lines = []
    lines.append(f"## Data e-shopu (provozovna {r.get('shop_id', '?')})")
    lines.append(f"Datum analýzy: {r.get('generated_at', '?')}")
    lines.append("")

    lines.append("### Položky")
    lines.append(f"- Celkem: {r.get('items_total', 0)}")
    lines.append(f"- V pořádku: {r.get('items_ok', 0)}")
    lines.append(f"- S chybou: {r.get('items_errors', 0)}")
    lines.append(f"- Ke zlepšení: {r.get('items_improvements', 0)}")
    lines.append(f"- Neviditelné: {r.get('items_not_visible', 0)}")
    lines.append(f"- Bez kategorie: {r.get('items_without_category', 0)}")
    lines.append(f"- Bez EAN: {r.get('items_no_ean', 0)}")
    lines.append(f"- Bez parametrů: {r.get('items_no_params', 0)}")
    lines.append(f"- Bez dostupnosti: {r.get('items_no_delivery', 0)}")
    lines.append(f"- Dražší než konkurence (>5%): {r.get('items_price_worse', 0)}")
    lines.append("")

    lines.append("### Výkon za 30 dní")
    lines.append(f"- Zobrazení: {r.get('perf_views', 0)}")
    lines.append(f"- Kliknutí: {r.get('perf_clicks', 0)}")
    lines.append(f"- CTR: {r.get('perf_ctr', 0)}%")
    lines.append(f"- Náklady: {r.get('perf_cost', 0)} Kč")
    lines.append(f"- Průměrné CPC: {r.get('perf_avg_cpc', 0)} Kč")
    lines.append(f"- Konverze: {r.get('perf_conversions', 0)}")
    lines.append(f"- Konverzní poměr: {r.get('perf_conv_rate', 0)}%")
    lines.append(f"- Hodnota konverzí: {r.get('perf_conv_value', 0)} Kč")
    pno = r.get('perf_pno', 0)
    lines.append(f"- PNO: {pno}%" if pno > 0 else "- PNO: nedostupné")
    lines.append("")

    # Konkurence
    cs = r.get('competition_summary', {})
    if cs:
        lines.append("### Konkurenční analýza")
        lines.append(f"- Průměrný počet eshopů na kartě: {cs.get('avgShopCount', '?')}")
        lines.append(f"- Median eshopů: {cs.get('medianShopCount', '?')}")
        lines.append(f"- Průměrný poměr ceny vs minimum: {cs.get('avgPriceVsMin', '?')}")
        lines.append(f"- Položky s nejlepší cenou: {cs.get('priceBetterThan10pct', '?')}")
        lines.append(f"- Položky o 10%+ dražší: {cs.get('priceWorseThan10pct', '?')}")
        lines.append("")

    # Recenze
    lines.append("### Recenze")
    lines.append(f"- Celkem: {r.get('reviews_total', 0)}")
    lines.append(f"- Pozitivní: {r.get('reviews_positive', 0)}")
    lines.append(f"- Negativní: {r.get('reviews_negative', 0)}")
    lines.append("")

    # Top kategorie
    cats = r.get('categories_analysis', [])
    if cats:
        lines.append("### Top kategorie (dle kliknutí)")
        for c in sorted(cats, key=lambda x: x.get('clicks', 0), reverse=True)[:10]:
            lines.append(
                f"- {c.get('category', '?')}: {c.get('clicks', 0)} kliků, "
                f"CTR {c.get('ctr', 0)}%, náklady {c.get('cost', 0)} Kč, "
                f"konverze {c.get('conversions', 0)}, "
                f"prům. eshopů {c.get('avgShopCount', '?')}, "
                f"cena vs min ×{c.get('avgPriceVsMin', '?')}"
            )
        lines.append("")

    # Doporučení (stávající)
    feed_recs = r.get('feed_recommendations', [])
    sklik_recs = r.get('sklik_recommendations', [])
    if feed_recs or sklik_recs:
        lines.append("### Stávající automatická doporučení")
        for rec in (feed_recs + sklik_recs)[:15]:
            lines.append(f"- [{rec.get('priority', '?')}] {rec.get('title', '')}: {rec.get('detail', '')[:100]}")
        lines.append("")

    # Feed info
    feeds = r.get('feeds_info', [])
    if feeds:
        lines.append("### Feed info")
        for f in feeds:
            lines.append(f"- URL: {f.get('feedUrl', f.get('url', '?'))}")
            lines.append(f"  Poslední import: {f.get('lastSuccessfulImportFormatted', '?')}")
        lines.append("")

    # Params analysis
    params = r.get('params_analysis', [])
    if params:
        with_issues = [p for p in params if p.get('missingCritical') or p.get('missingImportant')]
        if with_issues:
            lines.append("### Problémy s parametry")
            for p in with_issues[:10]:
                mc = p.get('missingCritical', [])
                mi = p.get('missingImportant', [])
                lines.append(f"- {p.get('category', '?')} ({p.get('itemCount', 0)} pol.): "
                             f"chybí kritické: {', '.join(mc[:5]) if mc else 'žádné'}, "
                             f"chybí důležité: {', '.join(mi[:5]) if mi else 'žádné'}")
            lines.append("")

    return "\n".join(lines)


@app.route("/export/csv", methods=["POST"])
def export_csv():
    body = request.get_json(silent=True) or {}
    data_type = body.get("type", "items")
    rows = body.get("data", [])

    if not rows:
        return jsonify({"error": "Žádná data k exportu"}), 400

    output = io.StringIO()
    if rows:
        keys = list(rows[0].keys())
        writer = csv.DictWriter(output, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            flat = {}
            for k in keys:
                v = row.get(k)
                if isinstance(v, (list, dict)):
                    flat[k] = json.dumps(v, ensure_ascii=False)
                else:
                    flat[k] = v
            writer.writerow(flat)

    csv_content = output.getvalue()
    return Response(
        csv_content,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=sklik_{data_type}.csv"},
    )


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5056))
    app.run(debug=os.environ.get("RAILWAY_ENVIRONMENT") is None, port=port, host="0.0.0.0")

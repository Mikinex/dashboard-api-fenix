import csv
import dataclasses
import io
import json
import math
import os
import queue
import traceback

from flask import Flask, render_template, request, jsonify, Response

from sklik_api import SklikAPI, SklikAPIError
from analyzer import SklikAnalyzer

# Načti .env pokud existuje
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)
app.config["JSON_ENSURE_ASCII"] = False

# Výchozí token z prostředí (pokud je nastaven v .env)
DEFAULT_API_TOKEN = os.environ.get("API_TOKEN", "")


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

    if not api_key:
        return jsonify({"error": "Zadejte API klíč."}), 400

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
            result_q.put(("ok", _to_dict(report)))
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

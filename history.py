"""
Historická data – SQLite úložiště pro snímky analýz.
Umožňuje porovnání v čase a cenový monitoring.
"""

import json
import os
import sqlite3
from datetime import datetime
from typing import Optional


DB_PATH = os.environ.get("HISTORY_DB", "history.db")


def _get_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    _ensure_tables(db)
    return db


def _ensure_tables(db: sqlite3.Connection):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            premise_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            items_total INTEGER,
            items_errors INTEGER,
            items_improvements INTEGER,
            perf_views INTEGER,
            perf_clicks INTEGER,
            perf_cost REAL,
            perf_conversions INTEGER,
            perf_avg_cpc REAL,
            perf_ctr REAL,
            perf_conv_rate REAL,
            perf_conv_value REAL,
            perf_pno REAL,
            reviews_total INTEGER,
            reviews_positive INTEGER,
            reviews_negative INTEGER,
            items_no_ean INTEGER,
            items_no_params INTEGER,
            items_no_delivery INTEGER,
            items_price_worse INTEGER,
            competition_avg_shop_count REAL,
            competition_avg_price_vs_min REAL,
            full_report TEXT
        );

        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            premise_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            item_name TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            price REAL,
            min_price_competitors REAL,
            shop_count INTEGER,
            top_position INTEGER,
            price_vs_min REAL
        );

        CREATE INDEX IF NOT EXISTS idx_snapshots_premise
            ON snapshots(premise_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_price_premise_item
            ON price_history(premise_id, item_id, created_at);
    """)
    db.commit()


def save_snapshot(premise_id: str, report_data: dict):
    """Uloží snímek analýzy do DB."""
    db = _get_db()
    cs = report_data.get("competition_summary", {})
    db.execute("""
        INSERT INTO snapshots (
            premise_id, items_total, items_errors, items_improvements,
            perf_views, perf_clicks, perf_cost, perf_conversions,
            perf_avg_cpc, perf_ctr, perf_conv_rate, perf_conv_value, perf_pno,
            reviews_total, reviews_positive, reviews_negative,
            items_no_ean, items_no_params, items_no_delivery, items_price_worse,
            competition_avg_shop_count, competition_avg_price_vs_min,
            full_report
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        premise_id,
        report_data.get("items_total", 0),
        report_data.get("items_errors", 0),
        report_data.get("items_improvements", 0),
        report_data.get("perf_views", 0),
        report_data.get("perf_clicks", 0),
        report_data.get("perf_cost", 0),
        report_data.get("perf_conversions", 0),
        report_data.get("perf_avg_cpc", 0),
        report_data.get("perf_ctr", 0),
        report_data.get("perf_conv_rate", 0),
        report_data.get("perf_conv_value", 0),
        report_data.get("perf_pno", 0),
        report_data.get("reviews_total", 0),
        report_data.get("reviews_positive", 0),
        report_data.get("reviews_negative", 0),
        report_data.get("items_no_ean", 0),
        report_data.get("items_no_params", 0),
        report_data.get("items_no_delivery", 0),
        report_data.get("items_price_worse", 0),
        cs.get("avgShopCount"),
        cs.get("avgPriceVsMin"),
        json.dumps(report_data, ensure_ascii=False, default=str),
    ))
    db.commit()

    # Uložit cenovou historii pro top položky
    items = report_data.get("raw_items", [])
    if items:
        _save_price_history(db, premise_id, items)

    db.close()


def _save_price_history(db: sqlite3.Connection, premise_id: str, items: list):
    """Uloží cenovou historii pro položky s konkurenčními daty."""
    rows = []
    for item in items:
        if item.get("price") is None:
            continue
        rows.append((
            premise_id,
            item.get("id", ""),
            item.get("name", ""),
            item.get("price"),
            item.get("minPriceCompetitors") or item.get("minPrice"),
            item.get("shopCount"),
            item.get("topPosition"),
            item.get("priceVsMin"),
        ))
    if rows:
        db.executemany("""
            INSERT INTO price_history (
                premise_id, item_id, item_name, price,
                min_price_competitors, shop_count, top_position, price_vs_min
            ) VALUES (?,?,?,?,?,?,?,?)
        """, rows)
        db.commit()


def get_snapshots(premise_id: str, limit: int = 30) -> list:
    """Vrátí historické snímky pro provozovnu."""
    db = _get_db()
    rows = db.execute("""
        SELECT id, created_at, items_total, items_errors, items_improvements,
               perf_views, perf_clicks, perf_cost, perf_conversions,
               perf_avg_cpc, perf_ctr, perf_conv_rate, perf_conv_value, perf_pno,
               reviews_total, items_no_ean, items_no_params, items_price_worse,
               competition_avg_shop_count, competition_avg_price_vs_min
        FROM snapshots
        WHERE premise_id = ?
        ORDER BY created_at DESC
        LIMIT ?
    """, (premise_id, limit)).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_price_history(premise_id: str, item_id: str, limit: int = 60) -> list:
    """Vrátí cenovou historii pro konkrétní položku."""
    db = _get_db()
    rows = db.execute("""
        SELECT created_at, price, min_price_competitors, shop_count,
               top_position, price_vs_min
        FROM price_history
        WHERE premise_id = ? AND item_id = ?
        ORDER BY created_at DESC
        LIMIT ?
    """, (premise_id, item_id, limit)).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_price_movers(premise_id: str, limit: int = 20) -> list:
    """Vrátí položky s největší změnou ceny konkurence od posledního snímku."""
    db = _get_db()
    rows = db.execute("""
        WITH latest AS (
            SELECT item_id, item_name, price, min_price_competitors,
                   shop_count, price_vs_min, created_at,
                   ROW_NUMBER() OVER (PARTITION BY item_id ORDER BY created_at DESC) AS rn
            FROM price_history
            WHERE premise_id = ?
        ),
        current AS (SELECT * FROM latest WHERE rn = 1),
        previous AS (SELECT * FROM latest WHERE rn = 2)
        SELECT
            c.item_id, c.item_name,
            c.price AS current_price,
            c.min_price_competitors AS current_min,
            p.min_price_competitors AS previous_min,
            c.shop_count,
            CASE WHEN p.min_price_competitors > 0
                THEN ROUND((c.min_price_competitors - p.min_price_competitors) / p.min_price_competitors * 100, 1)
                ELSE NULL END AS min_price_change_pct,
            c.created_at
        FROM current c
        JOIN previous p ON c.item_id = p.item_id
        WHERE p.min_price_competitors IS NOT NULL
          AND c.min_price_competitors IS NOT NULL
          AND c.min_price_competitors != p.min_price_competitors
        ORDER BY ABS(c.min_price_competitors - p.min_price_competitors) DESC
        LIMIT ?
    """, (premise_id, limit)).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_comparison(premise_id: str) -> Optional[dict]:
    """Porovná poslední dva snímky a vrátí změny."""
    snapshots = get_snapshots(premise_id, limit=2)
    if len(snapshots) < 2:
        return None

    current, previous = snapshots[0], snapshots[1]
    changes = {}
    compare_keys = [
        "items_total", "items_errors", "perf_views", "perf_clicks",
        "perf_cost", "perf_conversions", "perf_pno", "perf_ctr",
        "perf_avg_cpc", "items_price_worse", "items_no_ean", "items_no_params",
    ]
    for key in compare_keys:
        cur = current.get(key) or 0
        prev = previous.get(key) or 0
        diff = cur - prev
        pct = round((diff / prev) * 100, 1) if prev != 0 else None
        changes[key] = {"current": cur, "previous": prev, "diff": diff, "pct": pct}

    return {
        "current_date": current["created_at"],
        "previous_date": previous["created_at"],
        "changes": changes,
    }

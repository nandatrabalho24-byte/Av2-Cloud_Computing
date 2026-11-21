# pipeline test
"""
Dashboard de Indicadores Econômicos — App Flask
Lê os indicadores do Postgres da Aiven (tabela bcb_series). Os dados chegam
lá via cache batch: este próprio app busca dados novos na API do BCB quando
o cache está velho (ver refresh_stale_series abaixo).

Variáveis de ambiente esperadas (ver .env.example):
  AIVEN_DB_URL   -> string de conexão fornecida pela Aiven
  PORT           -> porta HTTP (default 8080)
  CACHE_HOURS    -> horas antes de buscar dados novos na API (default 24)
"""

import os
import time
import datetime
from flask import Flask, render_template, jsonify
import requests

import bcb_client as bcb

app = Flask(__name__)

CACHE_HOURS = int(os.environ.get("CACHE_HOURS", 24))
START_TIME = time.time()

# Garante que as tabelas existam assim que o módulo é carregado — necessário
# porque em produção o app roda via gunicorn (`gunicorn app:app`), que nunca
# executa o bloco `if __name__ == "__main__":` lá embaixo.
bcb.init_db()


def log_visit(path: str):
    with bcb.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO visits (path) VALUES (%s);", (path,))
        conn.commit()


def is_cache_stale(series_code: int) -> bool:
    with bcb.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MAX(fetched_at) AS last_fetch FROM bcb_series WHERE series_code = %s;",
                (series_code,),
            )
            row = cur.fetchone()
    if not row or not row["last_fetch"]:
        return True
    age = datetime.datetime.now(datetime.timezone.utc) - row["last_fetch"]
    return age > datetime.timedelta(hours=CACHE_HOURS)


def refresh_stale_series():
    """Cache batch: só busca na API do BCB se a série não foi atualizada
    recentemente (mais de CACHE_HOURS)."""
    for code in bcb.SERIES:
        if is_cache_stale(code):
            try:
                points = bcb.fetch_series_from_bcb(code)
                bcb.upsert_series(code, points)
            except (requests.RequestException, ValueError) as exc:  # noqa: BLE001
                app.logger.warning("Falha ao atualizar série %s: %s", code, exc)


def get_series_data(series_code: int):
    with bcb.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ref_date, value FROM (
                    SELECT ref_date, value
                    FROM bcb_series
                    WHERE series_code = %s
                    ORDER BY ref_date DESC
                    LIMIT %s
                ) recent
                ORDER BY ref_date ASC;
                """,
                (series_code, bcb.POINTS_PER_SERIES),
            )
            return cur.fetchall()


def get_dashboard_data():
    refresh_stale_series()

    indicators = []
    for code, meta in bcb.SERIES.items():
        rows = get_series_data(code)
        latest = rows[-1] if rows else None
        previous = rows[-2] if len(rows) > 1 else None
        delta = None
        if latest and previous:
            delta = round(float(latest["value"]) - float(previous["value"]), meta["casas"])

        values = [round(float(r["value"]), meta["casas"]) for r in rows]
        last12 = values[-12:]
        stats = None
        if values:
            stats = {
                "avg12": round(sum(last12) / len(last12), meta["casas"]),
                "min": min(values),
                "max": max(values),
                "min_label": rows[values.index(min(values))]["ref_date"].strftime("%m/%Y"),
                "max_label": rows[values.index(max(values))]["ref_date"].strftime("%m/%Y"),
                "year_delta": round(values[-1] - values[-13], meta["casas"]) if len(values) > 12 else None,
            }
        table = []
        for i in range(len(rows) - 1, max(len(rows) - 13, -1), -1):
            table.append(
                {
                    "label": rows[i]["ref_date"].strftime("%m/%Y"),
                    "value": values[i],
                    "delta": round(values[i] - values[i - 1], meta["casas"]) if i > 0 else None,
                }
            )

        indicators.append(
            {
                "code": code,
                "name": meta["name"],
                "desc": meta.get("desc", ""),
                "unit": meta["unit"],
                "latest_value": values[-1] if values else None,
                "latest_date": latest["ref_date"].strftime("%m/%Y") if latest else None,
                "delta": delta,
                "stats": stats,
                "table": table,
                "labels": [r["ref_date"].strftime("%m/%Y") for r in rows],
                "values": values,
            }
        )

    with bcb.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM visits;")
            total_visits = cur.fetchone()["total"]

    return {
        "indicators": indicators,
        "total_visits": total_visits,
        "uptime_seconds": int(time.time() - START_TIME),
        "server_time": datetime.datetime.utcnow().isoformat() + "Z",
        "cache_hours": CACHE_HOURS,
    }


@app.route("/")
def dashboard():
    log_visit("/")
    data = get_dashboard_data()
    return render_template("dashboard.html", data=data)


@app.route("/api/indicators")
def api_indicators():
    log_visit("/api/indicators")
    return jsonify(get_dashboard_data())


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Força a atualização de todas as séries via cache batch, ignorando a
    idade do cache. Chamado periodicamente pelo workflow refresh-data.yml."""
    for code in bcb.SERIES:
        try:
            points = bcb.fetch_series_from_bcb(code)
            bcb.upsert_series(code, points)
        except (requests.RequestException, ValueError) as exc:  # noqa: BLE001
            return jsonify({"status": "error", "series": code, "detail": str(exc)}), 502
    return jsonify({"status": "ok", "refreshed": list(bcb.SERIES.keys())})


@app.route("/healthz")
def healthz():
    """Usado pelo pipeline de CI/CD e pela plataforma de deploy (health check)."""
    try:
        with bcb.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
        return jsonify({"status": "ok"}), 200
    except Exception as exc:  # noqa: BLE001
        return jsonify({"status": "error", "detail": str(exc)}), 503


if __name__ == "__main__":
    refresh_stale_series()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

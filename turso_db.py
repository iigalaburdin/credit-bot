"""
Небольшая обёртка над HTTP API Turso — без сторонних библиотек с нативным кодом,
только requests. Формат запросов/ответов: https://docs.turso.tech/sdk/http/reference
"""

import os
import requests

TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")


def _pipeline_url():
    url = TURSO_DATABASE_URL.strip()
    # ссылка из панели Turso начинается с libsql:// — для HTTP API нужно https://
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://"):]
    url = url.rstrip("/")
    return url + "/v2/pipeline"


def _to_arg(value):
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    return {"type": "text", "value": str(value)}


def _from_cell(cell):
    t = cell.get("type")
    v = cell.get("value")
    if t == "integer":
        return int(v)
    if t == "float":
        return float(v)
    if t == "null":
        return None
    return v  # text / blob как есть


def execute(sql, args=None):
    """Выполняет один SQL-запрос. Возвращает список строк (списков python-значений)."""
    stmt = {"sql": sql}
    if args:
        stmt["args"] = [_to_arg(a) for a in args]

    body = {"requests": [{"type": "execute", "stmt": stmt}, {"type": "close"}]}
    resp = requests.post(
        _pipeline_url(),
        headers={
            "Authorization": f"Bearer {TURSO_AUTH_TOKEN}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    result_entry = data["results"][0]
    if result_entry.get("type") == "error":
        raise RuntimeError(result_entry.get("error"))

    result = result_entry["response"]["result"]
    rows = []
    for row in result.get("rows", []):
        rows.append([_from_cell(c) for c in row])
    return rows

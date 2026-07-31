#!/usr/bin/env python3
"""
FastAPI server for Someday Founder Web Pipeline.

Endpoints:
  GET  /monitor          → monitor.html
  GET  /browser          → browser.html
  GET  /api/stream       → SSE event stream (pipeline progress)
  POST /api/run          → start pipeline for a company
  GET  /api/stories      → list stories (filter: company, area; search; sort; page)
  GET  /api/companies    → list distinct companies
  GET  /api/areas        → list distinct areas

Usage:
  pip install fastapi uvicorn openai requests
  python sf_pipeline/server.py
"""

import asyncio
import json
import os
import sqlite3
import threading
from pathlib import Path
from queue import Empty, Queue

# load .env from sf_pipeline/.env or cwd/.env (whichever exists)
def _load_dotenv():
    for candidate in [Path(__file__).parent / ".env", Path(".env")]:
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            break

_load_dotenv()

import uvicorn
from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from sf_web_pipeline import run_pipeline, set_event_queue, init_db, enrich_story, enrich_story_web, call_gemini, _load_monthly_usage

BASE = Path(__file__).parent
DB_PATH = str(BASE / "data.db")
STATIC = BASE / "static"

app = FastAPI(title="Someday Founder Pipeline")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

# ── Global event queue shared with pipeline ───────────────────────────────────
_pipeline_queue: Queue = Queue()
set_event_queue(_pipeline_queue)

# ── SSE broadcast list ────────────────────────────────────────────────────────
_sse_clients: list[asyncio.Queue] = []
_sse_lock = threading.Lock()


def _broadcast(data: dict):
    msg = json.dumps(data)
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try:
                q.put_nowait(msg)
            except Exception:
                dead.append(q)
        for q in dead:
            _sse_clients.remove(q)


def _queue_relay():
    """Background thread: drain pipeline queue → broadcast to SSE clients."""
    while True:
        try:
            event = _pipeline_queue.get(timeout=1)
            # Track pause state so reconnecting clients can restore the UI
            t = event.get("type", "")
            if t == "step" and event.get("step") == "knowledge_refine" and event.get("status") == "done":
                _pipeline_context["paused_events"] = event.get("events_structured", [])
            elif t == "pipeline_paused":
                _pipeline_context["is_paused"] = True
            elif t in ("pipeline_resumed", "pipeline_done", "pipeline_stopped"):
                _pipeline_context["is_paused"] = False
            _broadcast(event)
        except Empty:
            continue


threading.Thread(target=_queue_relay, daemon=True).start()

# ── Pipeline state ────────────────────────────────────────────────────────────
_pipeline_running = False
_pipeline_lock = threading.Lock()
_stop_event = threading.Event()
_continue_event = threading.Event()
_pipeline_context: dict = {"selected_events": [], "is_paused": False, "paused_events": []}


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/monitor", response_class=HTMLResponse)
async def monitor():
    return (STATIC / "monitor.html").read_text()


@app.get("/browser", response_class=HTMLResponse)
async def browser():
    return (STATIC / "browser.html").read_text()


@app.get("/api/stream")
async def stream():
    client_q: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    with _sse_lock:
        _sse_clients.append(client_q)

    async def event_generator():
        try:
            yield "data: {\"type\":\"connected\"}\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(client_q.get(), timeout=15)
                    yield f"data: {msg}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            with _sse_lock:
                if client_q in _sse_clients:
                    _sse_clients.remove(client_q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/run")
async def run(body: dict):
    global _pipeline_running
    company = (body.get("company") or "").strip()
    llm = body.get("llm", "gpt4o-mini")
    n_queries = int(body.get("n_queries", 20))
    active_groups = body.get("active_groups") or None  # list of group slugs, None = all
    active_models = body.get("active_models") or None  # list of model keys, None = all

    if not company:
        return {"error": "company required"}

    with _pipeline_lock:
        if _pipeline_running:
            return {"error": "pipeline already running"}
        _pipeline_running = True

    _stop_event.clear()
    _continue_event.clear()

    def _run():
        global _pipeline_running
        try:
            run_pipeline(company, llm, n_queries=n_queries, db_path=DB_PATH,
                         stop_event=_stop_event, continue_event=_continue_event,
                         pipeline_context=_pipeline_context, active_groups=active_groups,
                         active_models=active_models)
        finally:
            _pipeline_running = False

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started", "company": company, "llm": llm}


@app.post("/api/stop")
async def stop():
    global _pipeline_running
    if not _pipeline_running:
        return {"status": "not_running"}
    _stop_event.set()
    _continue_event.set()  # unblock any waiting pause
    return {"status": "stopping"}


@app.post("/api/continue")
async def continue_pipeline(request: Request):
    try:
        body = await request.json()
        _pipeline_context["selected_events"] = body.get("selected_events", [])
    except Exception:
        _pipeline_context["selected_events"] = []
    _continue_event.set()
    return {"status": "continued"}


@app.get("/api/status")
async def status():
    return {
        "running": _pipeline_running,
        "paused": _pipeline_context.get("is_paused", False),
        "paused_events": _pipeline_context.get("paused_events", []),
    }


# ── Story Browser API ─────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@app.get("/api/stories")
async def stories(
    company: str = Query(default=""),
    area: str = Query(default=""),
    q: str = Query(default=""),
    sort: str = Query(default="created_at"),
    order: str = Query(default="desc"),
    page: int = Query(default=1),
    per_page: int = Query(default=50),
):
    allowed_sort = {"created_at", "company", "area", "principle"}
    sort = sort if sort in allowed_sort else "created_at"
    order = "DESC" if order.lower() == "desc" else "ASC"

    where = []
    params = []
    if company:
        where.append("company = ?")
        params.append(company)
    if area:
        where.append("area = ?")
        params.append(area)
    if q:
        where.append("(principle LIKE ? OR situation LIKE ? OR action LIKE ? OR result LIKE ?)")
        params.extend([f"%{q}%"] * 4)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    offset = (page - 1) * per_page

    conn = get_db()
    total = conn.execute(f"SELECT COUNT(*) FROM stories {where_sql}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM stories {where_sql} ORDER BY {sort} {order} LIMIT ? OFFSET ?",
        params + [per_page, offset],
    ).fetchall()
    conn.close()

    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "stories": [dict(r) for r in rows],
    }


@app.get("/api/companies")
async def companies():
    conn = get_db()
    rows = conn.execute("SELECT DISTINCT company FROM stories ORDER BY company").fetchall()
    conn.close()
    return [r[0] for r in rows]


@app.post("/api/enrich")
async def enrich(body: dict):
    story_id = body.get("story_id")
    if not story_id:
        return {"error": "story_id required"}
    conn = get_db()
    row = conn.execute("SELECT * FROM stories WHERE id = ?", [story_id]).fetchone()
    conn.close()
    if not row:
        return {"error": "Story not found"}
    result = await asyncio.get_event_loop().run_in_executor(None, enrich_story, dict(row))
    return result


@app.post("/api/enrich_web")
async def enrich_web(body: dict):
    story = body.get("story")
    if not story or not story.get("A"):
        return {"error": "story with A field required"}
    enrich_model = body.get("enrich_model", "gemini-3.6-flash")
    serper_key = os.environ.get("SERPER_API_KEY", "")
    tavily_key = os.environ.get("TAVILY_API_KEY", "")
    cheap_llm = lambda p, max_tokens=600: call_gemini(p, model="gemini-3-flash-preview", max_tokens=max_tokens)
    expensive_llm = lambda p, max_tokens=4000: call_gemini(p, model=enrich_model, max_tokens=max_tokens)
    result = await asyncio.get_event_loop().run_in_executor(
        None, enrich_story_web, story, serper_key, tavily_key, cheap_llm, expensive_llm
    )
    return result


@app.get("/api/usage_stats")
async def usage_stats():
    return _load_monthly_usage(DB_PATH)


@app.post("/api/clear_stories")
async def clear_stories():
    conn = get_db()
    conn.execute("DELETE FROM stories")
    conn.commit()
    conn.close()
    return {"status": "cleared"}


@app.get("/api/areas")
async def areas():
    conn = get_db()
    rows = conn.execute("SELECT DISTINCT area FROM stories ORDER BY area").fetchall()
    conn.close()
    return [r[0] for r in rows]


# ── Startup: ensure DB exists ─────────────────────────────────────────────────

@app.on_event("startup")
def startup():
    conn = init_db(DB_PATH)
    conn.close()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)

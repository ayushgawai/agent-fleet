"""FastAPI + htmx dashboard: stream status/logs, approve/retry/discard."""
from __future__ import annotations
import html, os, threading
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from .fleet import Fleet

app = FastAPI(title="agent-fleet")
_fleet: Fleet | None = None
def get_fleet() -> Fleet:
    global _fleet
    if _fleet is None: _fleet = Fleet(os.environ.get("FLEET_REPO", "."), sandbox_kind=os.environ.get("FLEET_SANDBOX", "subprocess"))
    return _fleet
def set_fleet(f):
    global _fleet; _fleet = f

class Submit(BaseModel):
    prompt: str
    agent: str = "shell:printf 'hello from agent\\n'; printf 'agent was here\\n' > AGENT.md"

@app.get("/api/tasks")
def tasks(): return [{"id": t.id, "prompt": t.prompt, "status": t.status, "branch": t.branch, "verdict": t.verdict, "error": t.error} for t in get_fleet().tasks.values()]
@app.get("/api/tasks/{tid}/logs")
def logs(tid: str): t = get_fleet().tasks[tid]; return {"logs": t.logs, "status": t.status}
@app.post("/api/tasks")
def submit(body: Submit):
    t = get_fleet().submit(body.prompt, body.agent); threading.Thread(target=get_fleet().run, args=(t,), daemon=True).start(); return {"id": t.id}
@app.post("/api/tasks/{tid}/{action}")
def act(tid: str, action: str):
    f = get_fleet()
    if action == "approve": f.approve(tid)
    elif action == "discard": f.discard(tid)
    elif action == "retry": threading.Thread(target=f.retry, args=(tid,), daemon=True).start()
    else: return {"error": "unknown action"}
    return {"ok": True}

PAGE = """<!doctype html><meta charset=utf-8><title>agent-fleet</title>
<script src="https://unpkg.com/htmx.org@1.9.12/dist/htmx.min.js"></script>
<style>body{font-family:system-ui;margin:2rem;max-width:1000px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:.4rem}pre{background:#111;color:#eee;padding:1rem;overflow:auto;max-height:300px}</style>
<h1>agent-fleet</h1>
<form hx-post="/api/tasks" hx-ext="json-enc" hx-on::after-request="this.reset();htmx.trigger('#tasks','refresh')">
<input name=prompt placeholder="Task prompt" size=60 required><button>Run agent</button></form>
<script src="https://unpkg.com/htmx.org@1.9.12/dist/ext/json-enc.js"></script>
<div id=tasks hx-get="/ui/tasks" hx-trigger="load, refresh, every 2s"></div>
<div id=logs></div>"""

@app.get("/", response_class=HTMLResponse)
def index(): return PAGE
@app.get("/ui/tasks", response_class=HTMLResponse)
def ui_tasks():
    rows = "".join(f"<tr><td>{t.id}</td><td>{html.escape(t.prompt)}</td><td>{t.status}</td><td>{html.escape(t.branch)}</td>"
                   f"<td><button hx-get='/ui/logs/{t.id}' hx-target='#logs'>logs</button> "
                   f"<button hx-post='/api/tasks/{t.id}/approve'>approve</button> <button hx-post='/api/tasks/{t.id}/retry'>retry</button> <button hx-post='/api/tasks/{t.id}/discard'>discard</button></td></tr>"
                   for t in get_fleet().tasks.values()) or "<tr><td colspan=5>No tasks yet</td></tr>"
    return f"<table><tr><th>id</th><th>prompt</th><th>status</th><th>branch</th><th>actions</th></tr>{rows}</table>"
@app.get("/ui/logs/{tid}", response_class=HTMLResponse)
def ui_logs(tid: str):
    t = get_fleet().tasks[tid]; return f"<h3>{tid} — {t.status}</h3><pre>{html.escape(chr(10).join(t.logs))}</pre>"
def main():
    import uvicorn; uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))

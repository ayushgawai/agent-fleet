"""FastAPI + htmx dashboard: stream status/logs, approve/retry/discard."""
from __future__ import annotations
import asyncio, html, json, os, threading
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from .fleet import Fleet, Task

DEFAULT_AGENT = "shell:printf 'hello from agent\\n'; printf 'agent was here\\n' > AGENT.md"
TERMINAL = {"awaiting_review", "merged", "failed", "discarded"}

app = FastAPI(title="agent-fleet")
_fleet: Fleet | None = None
def get_fleet() -> Fleet:
    global _fleet
    if _fleet is None: _fleet = Fleet(os.environ.get("FLEET_REPO", "."), sandbox_kind=os.environ.get("FLEET_SANDBOX", "auto"))
    return _fleet
def set_fleet(f):
    global _fleet; _fleet = f
def _task(tid: str) -> Task:
    t = get_fleet().tasks.get(tid)
    if t is None: raise HTTPException(404, f"no task {tid}")
    return t
def _info(t: Task) -> dict:
    return {"id": t.id, "prompt": t.prompt, "agent": t.agent, "status": t.status, "branch": t.branch,
            "sandbox": t.sandbox, "verdict": t.verdict, "error": t.error}

class Submit(BaseModel):
    prompt: str
    agent: str = DEFAULT_AGENT

@app.get("/api/tasks")
def tasks(): return [_info(t) for t in get_fleet().tasks.values()]
@app.get("/api/tasks/{tid}/logs")
def logs(tid: str): t = _task(tid); return {"logs": t.logs, "status": t.status}
@app.get("/api/tasks/{tid}/stream")
async def stream(tid: str):
    """Server-sent events: one ``log`` event per output line, ``status`` on change; ends at a terminal status."""
    t = _task(tid)
    async def events():
        sent, last = 0, None
        while True:
            status = t.status  # read before draining so lines written before a final status are not lost
            while sent < len(t.logs):
                yield f"event: log\ndata: {json.dumps(t.logs[sent])}\n\n"; sent += 1
            if status != last:
                yield f"event: status\ndata: {json.dumps(status)}\n\n"; last = status
            if status in TERMINAL: return
            await asyncio.sleep(0.25)
    return StreamingResponse(events(), media_type="text/event-stream")
@app.post("/api/tasks")
def submit(body: Submit):
    f = get_fleet(); t = f.submit(body.prompt, body.agent or DEFAULT_AGENT)
    threading.Thread(target=f.run, args=(t,), daemon=True).start(); return {"id": t.id}
@app.post("/api/tasks/{tid}/{action}")
def act(tid: str, action: str):
    f = get_fleet(); _task(tid)
    try:
        if action == "approve": f.approve(tid)
        elif action == "discard": f.discard(tid)
        elif action == "retry": threading.Thread(target=f.retry, args=(tid,), daemon=True).start()
        else: raise HTTPException(400, f"unknown action {action}")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>agent-fleet</title>
<script src="https://unpkg.com/htmx.org@1.9.12/dist/htmx.min.js"></script>
<script src="https://unpkg.com/htmx.org@1.9.12/dist/ext/json-enc.js"></script>
<style>body{font-family:system-ui;margin:2rem auto;max-width:1100px;padding:0 1rem}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:.4rem;text-align:left}
pre{background:#111;color:#eee;padding:1rem;overflow:auto;max-height:320px}input{padding:.3rem}.blocked{color:#b00;font-weight:600}.ok{color:#070}</style>
<h1>agent-fleet</h1>
<form hx-post="/api/tasks" hx-ext="json-enc" hx-swap="none" hx-on::after-request="this.prompt.value='';htmx.trigger('#tasks','refresh')">
<input name=prompt placeholder="Task prompt" size=40 required> <input name=agent placeholder="agent spec (default: demo shell agent)" size=40> <button>Run agent</button></form>
<div id=tasks hx-get="/ui/tasks" hx-trigger="load, refresh, every 2s"></div>
<div id=logs></div>
<script>
let es; function follow(id){ if(es) es.close(); const pre=document.getElementById('log-'+id); pre.textContent='';
 es=new EventSource('/api/tasks/'+id+'/stream');
 es.addEventListener('log',e=>{pre.textContent+=JSON.parse(e.data)+'\\n'; pre.scrollTop=pre.scrollHeight});
 es.addEventListener('status',e=>{document.getElementById('st-'+id).textContent=JSON.parse(e.data); if(['awaiting_review','merged','failed','discarded'].includes(JSON.parse(e.data))) es.close()}); }
document.body.addEventListener('htmx:afterSwap',e=>{const p=e.detail.target.querySelector('pre[data-task]'); if(p) follow(p.dataset.task)});
</script>"""

def _verdict_cell(t: Task) -> str:
    if not t.verdict: return html.escape(t.error)
    n = len(t.verdict["findings"])
    return f"<span class=blocked>blocked ({n})</span>" if t.verdict["blocked"] else f"<span class=ok>{n} finding(s), max {t.verdict['max_severity']}</span>"

@app.get("/", response_class=HTMLResponse)
def index(): return PAGE
@app.get("/ui/tasks", response_class=HTMLResponse)
def ui_tasks():
    rows = "".join(f"<tr><td>{t.id}</td><td>{html.escape(t.prompt)}</td><td>{t.status}</td><td>{html.escape(t.sandbox)}</td><td>{html.escape(t.branch)}</td><td>{_verdict_cell(t)}</td>"
                   f"<td><button hx-get='/ui/logs/{t.id}' hx-target='#logs'>logs</button> "
                   + "".join(f"<button hx-post='/api/tasks/{t.id}/{a}' hx-swap=none>{a}</button> " for a in ("approve", "retry", "discard"))
                   + "</td></tr>" for t in get_fleet().tasks.values()) or "<tr><td colspan=7>No tasks yet</td></tr>"
    return f"<table><tr><th>id</th><th>prompt</th><th>status</th><th>sandbox</th><th>branch</th><th>review</th><th>actions</th></tr>{rows}</table>"
@app.get("/ui/logs/{tid}", response_class=HTMLResponse)
def ui_logs(tid: str):
    t = _task(tid)
    findings = "".join(f"<li><b>{f['severity']}</b> {html.escape(f['rule'])} {html.escape(f['path'])}: {html.escape(f['message'])}</li>"
                       for f in (t.verdict or {}).get("findings", []))
    return (f"<h3>{tid} — <span id=st-{tid}>{t.status}</span></h3><pre id=log-{tid} data-task={tid}>{html.escape(chr(10).join(t.logs))}</pre>"
            + (f"<ul>{findings}</ul>" if findings else ""))

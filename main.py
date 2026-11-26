"""
Yeeps v1 Private Server API
FastAPI + MongoDB replacement for the original AWS Lambda + DynamoDB backend.

The game client sends requests to three "services" all on the same host.
We detect which service by inspecting the path, headers, and host.
"""

import json
import re
import os
from datetime import datetime, timezone
from pathlib import Path

from starlette.concurrency import iterate_in_threadpool

from fastapi import FastAPI, Request
from fastapi.responses import Response, JSONResponse

from routers.lambda_router import router as lambda_router
from routers.dynamo_router import router as dynamo_router
from seed.seed_db import seed_database
from services.replay_log import lookup_replay
from services.db import accounts

app = FastAPI(title="Yeeps v1 Private Server")
HTTP_LOG_FILE = Path("logs/http_traffic.log")


def _safe_decode(payload: bytes) -> str:
    """Decode bytes for logging without throwing on binary data."""
    if not payload:
        return ""
    return payload.decode("utf-8", errors="replace")


def _append_http_log(entry: str) -> None:
    HTTP_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with HTTP_LOG_FILE.open("a", encoding="utf-8") as log_file:
        log_file.write(entry)


@app.middleware("http")
async def log_http_traffic(request: Request, call_next):
    request_body = await request.body()
    request_body_text = _safe_decode(request_body)

    response = await call_next(request)

    response_chunks = [chunk async for chunk in response.body_iterator]
    response_body = b"".join(response_chunks)
    response_body_text = _safe_decode(response_body)

    # Rebuild response because we consumed body_iterator while logging.
    response.body_iterator = iterate_in_threadpool(iter([response_body]))

    now = datetime.now(timezone.utc).isoformat()
    full_path = str(request.url.path)
    if request.url.query:
        full_path = f"{full_path}?{request.url.query}"

    # Don't log the debug page itself, so it doesn't fill the log.
    if not request.url.path.startswith("/__recent"):
        # Show headers that tell us which AWS service the game is calling.
        interesting = {
            k: v for k, v in request.headers.items()
            if k.lower() in ("x-amz-target", "content-type", "user-agent", "host")
        }
        log_entry = (
            f"[{now}]\n"
            f"Client: {request.client.host if request.client else 'unknown'}\n"
            f"Method: {request.method}\n"
            f"Path: {full_path}\n"
            f"Headers: {json.dumps(interesting)}\n"
            f"RequestBody: {request_body_text}\n"
            f"StatusCode: {response.status_code}\n"
            f"ResponseBody: {response_body_text}\n"
            f"{'=' * 80}\n"
        )
        _append_http_log(log_entry)

    return response


# ─── Startup ─────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup_event():
    """Optionally seed the database on startup."""
    if os.getenv("SEED_DB", "").strip().lower() in {"1", "true", "yes", "y", "on"}:
        await seed_database()
    else:
        print("[seed] Skipped (set SEED_DB=1 to enable)")
    print("=" * 60)
    print("  Yeeps v1 Private Server is READY")
    print("=" * 60)


# ─── Include routers ─────────────────────────────────────────────────

# Lambda routes (specific path pattern)
app.include_router(lambda_router)


@app.get("/__recent")
async def recent_requests(key: str = "", n: int = 40):
    """Show the last n logged requests. Needs ?key=<DEBUG_KEY>."""
    secret = os.getenv("DEBUG_KEY", "")
    if not secret or key != secret:
        return JSONResponse(status_code=404, content={})
    if not HTTP_LOG_FILE.exists():
        return Response("No requests logged yet.", media_type="text/plain")
    sep = "=" * 80 + "\n"
    entries = HTTP_LOG_FILE.read_text(encoding="utf-8").split(sep)
    return Response(sep.join(entries[-(n + 1):]), media_type="text/plain")


# ─── Startup checks the game makes before logging in ─────────────────

async def handle_fetch_global_fatal_error(body: dict) -> dict:
    """
    The game calls this first, to ask "is there a server-wide error?".
    Answer: no error, so the game carries on to log in.
    """
    return {"data": {}}


# Exact routes the game hits on the API URL (matched case-insensitively,
# anywhere in the path, so /v1/fetchGlobalFatalError works too).
STARTUP_HANDLERS = [
    ("fetchglobalfatalerror", handle_fetch_global_fatal_error),
]


# ─── Catch-all route ─────────────────────────────────────────────────

@app.api_route("/", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def catch_all(request: Request, path: str = ""):
    """
    Catch-all route that detects which AWS service the game client
    is trying to reach and dispatches accordingly.
    """
    host = request.headers.get("host", "")
    target = request.headers.get("x-amz-target", "")
    auth = request.headers.get("authorization", "")
    method = request.method

    print(f"\n{'='*60}")
    print(f"{method} /{path}")
    print(f"Host: {host}")

    # ── 1. Photon Auth: GET / with query string ──
    if method == "GET" and path == "" and str(request.query_params):
        q = request.query_params
        account_id = q.get("UserId") or q.get("userId") or q.get("accountID") or "o_0"
        username = q.get("UserName") or q.get("userName") or q.get("username")
        if not username and account_id:
            account_doc = await accounts.find_one({"accountID": account_id}, {"_id": 0, "displayName": 1})
            if account_doc and isinstance(account_doc.get("displayName"), str) and account_doc["displayName"].strip():
                username = account_doc["displayName"].strip()
        if not username:
            username = account_id
        payload = {"ResultCode": 1, "UserId": f"{account_id}$VR${username}"}
        print(f"Service: Photon Auth → {payload}")
        print(f"{'='*60}\n")
        return JSONResponse(content=payload)

    # Parse body for POST/PUT requests
    raw = await request.body()
    raw_text = _safe_decode(raw)

    # Replay exact responses captured in server.log when possible.
    replay = lookup_replay(method, f"/{path}" if path else "/", raw_text)
    if replay is not None:
        print("Service: Replay | source: server.log")
        print(f"→ {replay['status_code']} {replay['content_type']}")
        print(f"{'='*60}\n")
        return Response(
            content=replay["response_body"],
            status_code=replay["status_code"],
            media_type=replay["content_type"],
        )

    try:
        body = json.loads(raw) if raw else {}
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    # ── 2. Lambda: path matches invocation pattern ──
    # (search, not match, so a prefix like /v1/ or /a/ in front still works)
    m = re.search(r"2015-03-31/functions/([^/]+)/invocations/?$", path)
    if m or "/lambda/" in auth or host.startswith("lambda"):
        fn = m.group(1) if m else (path or "unknown")
        print(f"Service: Lambda | function: {fn}")
        # Forward to the lambda router handler directly
        from routers.lambda_router import lambda_invoke
        return await lambda_invoke(fn, request)

    # ── 3. DynamoDB: X-Amz-Target header present ──
    if (target and target.startswith("DynamoDB_")) or "/dynamodb/" in auth or host.startswith("dynamodb"):
        op = target.rsplit(".", 1)[-1] if "." in target else "?"
        tbl = body.get("TableName", "")
        if not tbl:
            ri = body.get("RequestItems", {})
            if isinstance(ri, dict) and ri:
                tbl = ",".join(ri.keys())
        print(f"Service: DynamoDB | op: {op} | table: {tbl}")

        from services.dynamo_ops import dispatch_dynamo
        payload_text, headers = await dispatch_dynamo(target, body)
        print(f"→ 200 application/x-amz-json-1.0")
        print(f"{'='*60}\n")
        return Response(
            content=payload_text,
            media_type="application/x-amz-json-1.0",
            headers=headers,
        )

    fn_lower = path.lower()

    # ── 4a. Startup checks (fetchGlobalFatalError, ...) ──
    for match_str, handler in STARTUP_HANDLERS:
        if match_str in fn_lower:
            print(f"Service: Startup | route: {path} | handler: {handler.__name__}")
            result = await handler(body)
            print(f"→ 200 application/json")
            print(f"{'='*60}\n")
            return JSONResponse(content=result)

    # ── 4b. Direct API: path matches known handler ──
    # This handles direct endpoints like /questLogIntoAccount
    from routers.lambda_router import HANDLER_TABLE
    for match_str, handler in HANDLER_TABLE:
        if match_str in fn_lower:
            print(f"Service: Direct API | route: {path} | handler: {handler.__name__}")
            result = await handler(body)
            # Ensure "data" wrapper exists
            if isinstance(result, dict) and "data" not in result:
                result = {"data": result}
            print(f"→ 200 application/json")
            print(f"{'='*60}\n")
            return JSONResponse(content=result)

    # ── 5. Unknown API call — answer with an empty "data" so the game keeps going ──
    if path and method != "GET":
        print(f"Service: (unknown) {path} — returning {{\"data\": {{}}}}")
        print(f"{'='*60}\n")
        return JSONResponse(content={"data": {}})

    # Plain browser visits (GET /, /favicon.ico, ...)
    print(f"Service: (unknown) — returning {{}}")
    print(f"{'='*60}\n")
    return JSONResponse(content={})


# ─── Entry point ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    from config import SERVER_PORT
    uvicorn.run("main:app", host="0.0.0.0", port=SERVER_PORT, reload=True)

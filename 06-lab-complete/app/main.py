"""
Production AI Agent — Kết hợp tất cả Day 12 concepts

Checklist:
  ✅ Config từ environment (12-factor)
  ✅ Structured JSON logging
  ✅ API Key authentication
  ✅ Rate limiting (sliding window)
  ✅ Cost guard (daily budget)
  ✅ Input validation (Pydantic)
  ✅ Health check + Readiness probe
  ✅ Graceful shutdown (SIGTERM)
  ✅ Security headers
  ✅ CORS
  ✅ Error handling
  ✅ Stateless design (Redis-backed session/history)
  ✅ Conversation history (multi-turn)
"""
import os
import time
import signal
import logging
import json
import uuid
from datetime import datetime, timezone
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Security, Depends, Request, Response
from fastapi.security.api_key import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

from app.config import settings

from utils.mock_llm import ask as llm_ask

# ─────────────────────────────────────────────────────────
# Logging — JSON structured
# ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format='{"ts":"%(asctime)s","lvl":"%(levelname)s","msg":"%(message)s"}',
)
logger = logging.getLogger(__name__)

START_TIME = time.time()
_is_ready = False
_request_count = 0
_error_count = 0
INSTANCE_ID = os.getenv("INSTANCE_ID", f"instance-{uuid.uuid4().hex[:6]}")

# ─────────────────────────────────────────────────────────
# Redis Connection (stateless storage)
# ─────────────────────────────────────────────────────────
_redis = None
USE_REDIS = False

def _init_redis():
    global _redis, USE_REDIS
    if not settings.redis_url:
        logger.warning("REDIS_URL not set — using in-memory store (not scalable!)")
        return
    try:
        import redis
        _redis = redis.from_url(settings.redis_url, decode_responses=True)
        _redis.ping()
        USE_REDIS = True
        logger.info(json.dumps({"event": "redis_connected", "url": settings.redis_url}))
    except Exception as e:
        logger.warning(json.dumps({"event": "redis_failed", "error": str(e)}))

# In-memory fallback (for development without Redis)
_memory_store: dict = {}

def _store_set(key: str, value: str, ttl: int = 3600):
    if USE_REDIS:
        _redis.setex(key, ttl, value)
    else:
        _memory_store[key] = value

def _store_get(key: str) -> str | None:
    if USE_REDIS:
        return _redis.get(key)
    return _memory_store.get(key)

def _store_delete(key: str):
    if USE_REDIS:
        _redis.delete(key)
    else:
        _memory_store.pop(key, None)

def _store_lrange(key: str, start: int, end: int) -> list:
    if USE_REDIS:
        return _redis.lrange(key, start, end)
    return _memory_store.get(key, [])

def _store_rpush(key: str, value: str):
    if USE_REDIS:
        _redis.rpush(key, value)
        _redis.expire(key, 3600)
    else:
        if key not in _memory_store:
            _memory_store[key] = []
        _memory_store[key].append(value)

def _store_ltrim(key: str, start: int, end: int):
    if USE_REDIS:
        _redis.ltrim(key, start, end)
    elif key in _memory_store:
        _memory_store[key] = _memory_store[key][start:end+1]

# ─────────────────────────────────────────────────────────
# Session & Conversation History (Stateless — Redis-backed)
# ─────────────────────────────────────────────────────────

def save_session(session_id: str, data: dict, ttl: int = 3600):
    _store_set(f"session:{session_id}", json.dumps(data), ttl)

def load_session(session_id: str) -> dict:
    raw = _store_get(f"session:{session_id}")
    return json.loads(raw) if raw else {}

def append_to_history(session_id: str, role: str, content: str):
    entry = json.dumps({
        "role": role,
        "content": content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    _store_rpush(f"history:{session_id}", entry)
    entries = _store_lrange(f"history:{session_id}", 0, -1)
    if len(entries) > 20:
        _store_ltrim(f"history:{session_id}", -20, -1)

def get_history(session_id: str) -> list[dict]:
    entries = _store_lrange(f"history:{session_id}", 0, -1)
    return [json.loads(e) for e in entries]

def delete_session_data(session_id: str):
    _store_delete(f"session:{session_id}")
    _store_delete(f"history:{session_id}")

# ─────────────────────────────────────────────────────────
# In-memory Rate Limiter (sliding window)
# ─────────────────────────────────────────────────────────
_rate_windows: dict[str, deque] = defaultdict(deque)

def check_rate_limit(key: str):
    now = time.time()
    window = _rate_windows[key]
    while window and window[0] < now - 60:
        window.popleft()
    if len(window) >= settings.rate_limit_per_minute:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {settings.rate_limit_per_minute} req/min",
            headers={"Retry-After": "60"},
        )
    window.append(now)

# ─────────────────────────────────────────────────────────
# Cost Guard
# ─────────────────────────────────────────────────────────
_daily_cost = 0.0
_cost_reset_day = time.strftime("%Y-%m-%d")

def check_and_record_cost(input_tokens: int, output_tokens: int):
    global _daily_cost, _cost_reset_day
    today = time.strftime("%Y-%m-%d")
    if today != _cost_reset_day:
        _daily_cost = 0.0
        _cost_reset_day = today
    if _daily_cost >= settings.daily_budget_usd:
        raise HTTPException(503, "Daily budget exhausted. Try tomorrow.")
    cost = (input_tokens / 1000) * 0.00015 + (output_tokens / 1000) * 0.0006
    _daily_cost += cost

# ─────────────────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────────────────
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def verify_api_key(api_key: str = Security(api_key_header)) -> str:
    if not api_key or api_key != settings.agent_api_key:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key. Include header: X-API-Key: <key>",
        )
    return api_key

# ─────────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _is_ready
    _init_redis()
    logger.info(json.dumps({
        "event": "startup",
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "instance": INSTANCE_ID,
        "storage": "redis" if USE_REDIS else "in-memory",
    }))
    time.sleep(0.1)
    _is_ready = True
    logger.info(json.dumps({"event": "ready", "instance": INSTANCE_ID}))

    yield

    _is_ready = False
    logger.info(json.dumps({"event": "shutdown", "instance": INSTANCE_ID}))

# ─────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/docs" if settings.environment != "production" else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)

@app.middleware("http")
async def request_middleware(request: Request, call_next):
    global _request_count, _error_count
    start = time.time()
    _request_count += 1
    try:
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers.pop("server", None)
        duration = round((time.time() - start) * 1000, 1)
        logger.info(json.dumps({
            "event": "request",
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "ms": duration,
            "instance": INSTANCE_ID,
        }))
        return response
    except Exception as e:
        _error_count += 1
        raise

# ─────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────
class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000,
                          description="Your question for the agent")

class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    session_id: str | None = Field(None, description="Session ID for multi-turn. Omit to create new.")

class AskResponse(BaseModel):
    question: str
    answer: str
    model: str
    timestamp: str

class ChatResponse(BaseModel):
    session_id: str
    question: str
    answer: str
    turn: int
    served_by: str
    storage: str

# ─────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────

@app.get("/", tags=["Info"])
def root():
    return {
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "instance": INSTANCE_ID,
        "storage": "redis" if USE_REDIS else "in-memory",
        "endpoints": {
            "ask": "POST /ask (requires X-API-Key)",
            "chat": "POST /chat (multi-turn, requires X-API-Key)",
            "health": "GET /health",
            "ready": "GET /ready",
        },
    }


@app.post("/ask", response_model=AskResponse, tags=["Agent"])
async def ask_agent(
    body: AskRequest,
    request: Request,
    _key: str = Depends(verify_api_key),
):
    check_rate_limit(_key[:8])

    input_tokens = len(body.question.split()) * 2
    check_and_record_cost(input_tokens, 0)

    logger.info(json.dumps({
        "event": "agent_call",
        "q_len": len(body.question),
        "client": str(request.client.host) if request.client else "unknown",
        "instance": INSTANCE_ID,
    }))

    answer = llm_ask(body.question)

    output_tokens = len(answer.split()) * 2
    check_and_record_cost(0, output_tokens)

    return AskResponse(
        question=body.question,
        answer=answer,
        model=settings.llm_model,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@app.post("/chat", response_model=ChatResponse, tags=["Agent"])
async def chat(
    body: ChatRequest,
    request: Request,
    _key: str = Depends(verify_api_key),
):
    check_rate_limit(_key[:8])

    input_tokens = len(body.question.split()) * 2
    check_and_record_cost(input_tokens, 0)

    session_id = body.session_id or str(uuid.uuid4())

    append_to_history(session_id, "user", body.question)

    answer = llm_ask(body.question)

    output_tokens = len(answer.split()) * 2
    check_and_record_cost(0, output_tokens)

    append_to_history(session_id, "assistant", answer)

    history = get_history(session_id)
    turn = len([m for m in history if m["role"] == "user"])

    logger.info(json.dumps({
        "event": "chat",
        "session_id": session_id,
        "turn": turn,
        "instance": INSTANCE_ID,
    }))

    return ChatResponse(
        session_id=session_id,
        question=body.question,
        answer=answer,
        turn=turn,
        served_by=INSTANCE_ID,
        storage="redis" if USE_REDIS else "in-memory",
    )


@app.get("/chat/{session_id}/history", tags=["Agent"])
def get_chat_history(session_id: str, _key: str = Depends(verify_api_key)):
    history = get_history(session_id)
    if not history:
        raise HTTPException(404, f"Session {session_id} not found or expired")
    return {
        "session_id": session_id,
        "messages": history,
        "count": len(history),
    }


@app.delete("/chat/{session_id}", tags=["Agent"])
def delete_chat(session_id: str, _key: str = Depends(verify_api_key)):
    delete_session_data(session_id)
    return {"deleted": session_id}


@app.get("/health", tags=["Operations"])
def health():
    redis_ok = False
    if USE_REDIS:
        try:
            _redis.ping()
            redis_ok = True
        except Exception:
            redis_ok = False

    status = "ok" if (not USE_REDIS or redis_ok) else "degraded"
    checks = {
        "llm": "mock" if not settings.openai_api_key else "openai",
        "redis": "connected" if redis_ok else ("not_configured" if not USE_REDIS else "disconnected"),
    }
    return {
        "status": status,
        "version": settings.app_version,
        "environment": settings.environment,
        "instance": INSTANCE_ID,
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "total_requests": _request_count,
        "checks": checks,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/ready", tags=["Operations"])
def ready():
    if not _is_ready:
        raise HTTPException(503, "Not ready")
    if USE_REDIS:
        try:
            _redis.ping()
        except Exception:
            raise HTTPException(503, "Redis not available")
    return {"ready": True, "instance": INSTANCE_ID}


@app.get("/metrics", tags=["Operations"])
def metrics(_key: str = Depends(verify_api_key)):
    return {
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "total_requests": _request_count,
        "error_count": _error_count,
        "daily_cost_usd": round(_daily_cost, 4),
        "daily_budget_usd": settings.daily_budget_usd,
        "budget_used_pct": round(_daily_cost / settings.daily_budget_usd * 100, 1),
        "instance": INSTANCE_ID,
        "storage": "redis" if USE_REDIS else "in-memory",
    }


# ─────────────────────────────────────────────────────────
# Graceful Shutdown
# ─────────────────────────────────────────────────────────
def _handle_signal(signum, _frame):
    logger.info(json.dumps({"event": "signal", "signum": signum, "instance": INSTANCE_ID}))

signal.signal(signal.SIGTERM, _handle_signal)


if __name__ == "__main__":
    logger.info(f"Starting {settings.app_name} on {settings.host}:{settings.port}")
    logger.info(f"API Key: {settings.agent_api_key[:4]}****")
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        timeout_graceful_shutdown=30,
    )

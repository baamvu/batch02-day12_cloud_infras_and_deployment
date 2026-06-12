# Solution.md — Day 12 Code Lab Answers

## Deployed URL

**Public URL:** https://ai-agent-production-tcv4.onrender.com/

**Endpoints:**
- `GET /health` — Liveness probe
- `GET /ready` — Readiness probe
- `POST /ask` — Agent endpoint (requires `X-API-Key` header)
- `POST /chat` — Multi-turn conversation (requires `X-API-Key` header)

---

## Part 1: Localhost vs Production

### Exercise 1.1: Phát hiện anti-patterns trong `01-localhost-vs-production/develop/app.py`

**5+ vấn đề tìm được:**

| # | Vấn đề | Dòng code | Tại sao nguy hiểm? |
|---|--------|-----------|---------------------|
| 1 | **API key hardcode** | `OPENAI_API_KEY = "sk-hardcoded-fake-key-never-do-this"` (line 17) | Nếu push lên GitHub, key bị lộ ngay lập tức. Attacker có thể dùng key để gọi API → mất tiền. |
| 2 | **Database URL hardcode** | `DATABASE_URL = "postgresql://admin:password123@localhost:5432/mydb"` (line 18) | Password database nằm trong source code. Ai đọc code đều biết credentials. |
| 3 | **Debug mode bật** | `DEBUG = True` (line 21) | Trong production, debug mode expose stack traces, chi tiết internal cho attacker. |
| 4 | **Không có health check** | Không có `/health` endpoint | Platform (Railway, Render, K8s) không biết container còn sống hay không → không tự restart khi crash. |
| 5 | **Port cố định** | `port=8000` hardcode (line 52) | Trên Railway/Render, PORT được inject qua env var. Hardcode sẽ conflict. |
| 6 | **Host chỉ localhost** | `host="localhost"` (line 51) | Container không accept kết nối từ bên ngoài → không ai truy cập được. |
| 7 | **Reload trong production** | `reload=True` (line 53) | File watcher tốn CPU, không an toàn cho production. |
| 8 | **Log secrets** | `print(f"[DEBUG] Using key: {OPENAI_API_KEY}")` (line 34) | Log ra API key → ai đọc logs đều thấy secret. |
| 9 | **Không xử lý shutdown** | Không có signal handler | Container bị kill đột ngột → request đang xử lý bị drop. |

### Exercise 1.2: Chạy basic version

```bash
cd 01-localhost-vs-production/develop
pip install -r requirements.txt
python app.py
# Test: curl -X POST "http://localhost:8000/ask?question=hello"
```

**Quan sát:** App chạy được, nhưng:
- Không có `/health` endpoint
- Log ra secrets
- Chỉ bind `localhost` (không chạy được trong Docker)
- Không có error handling

### Exercise 1.3: So sánh với advanced version

| Feature | Basic (`develop/app.py`) | Advanced (`production/app.py`) | Tại sao quan trọng? |
|---------|--------------------------|-------------------------------|---------------------|
| **Config** | Hardcode (`DEBUG=True`, port 8000) | Env vars (`settings.debug`, `settings.port`) | Thay đổi config mà không cần sửa code. Different config per environment. |
| **Health check** | Không có | `GET /health` + `GET /ready` | Platform biết container sống/chết → tự restart. Load balancer biết instance sẵn sàng. |
| **Logging** | `print()` + log secrets | JSON structured logging, không log secrets | Dễ parse bởi log aggregator (Datadog, Loki). Không lộ secrets. |
| **Shutdown** | Đột ngột (không xử lý) | Graceful shutdown với `lifespan` + `signal.SIGTERM` | Hoàn thành request trước khi tắt → không mất data. |
| **Binding** | `localhost` | `0.0.0.0` | Accept kết nối từ bên ngoài container. |
| **CORS** | Không có | Configurable `CORSMiddleware` | Kiểm soát domain nào được gọi API. |
| **Error handling** | Không có | `HTTPException` với status code rõ ràng | Client biết lỗi gì → xử lý đúng cách. |

### Checkpoint 1

- [x] Hiểu tại sao hardcode secrets là nguy hiểm — vì code có thể bị leak qua Git, logs, error messages
- [x] Biết cách dùng environment variables — `os.getenv("KEY", "default")` hoặc `pydantic-settings`
- [x] Hiểu vai trò của health check endpoint — platform dùng để monitor container, auto-restart khi crash
- [x] Biết graceful shutdown là gì — hoàn thành in-flight requests trước khi tắt, không mất data

---

## Part 2: Docker Containerization

### Exercise 2.1: Dockerfile cơ bản (`02-docker/develop/Dockerfile`)

**Câu trả lời:**

1. **Base image là gì?**
   - `python:3.11` — full Python distribution (~1GB). Bao gồm Python + pip + build tools.
   - So với `python:3.11-slim` (~150MB) chỉ có runtime tối thiểu.

2. **Working directory là gì?**
   - `WORKDIR /app` — tất cả commands sau đó chạy trong `/app`. Nếu không có, files sẽ nằm ở root `/`.

3. **Tại sao COPY requirements.txt trước?**
   - **Docker layer cache**: Nếu code thay đổi nhưng requirements không đổi, Docker dùng cache layer đã build → build nhanh hơn nhiều.
   - Nếu COPY tất cả trước, mỗi lần code thay đổi phải reinstall hết dependencies.

4. **CMD vs ENTRYPOINT khác nhau thế nào?**
   - `CMD ["python", "app.py"]` — command mặc định, có thể override khi `docker run`
   - `ENTRYPOINT ["python", "app.py"]` — command bắt buộc, `docker run` chỉ thêm args
   - Thường dùng ENTRYPOINT cho command chính, CMD cho default args

### Exercise 2.2: Build và run

```bash
cd ../../
docker build -f 02-docker/develop/Dockerfile -t my-agent:develop .
docker run -p 8000:8000 my-agent:develop
# Test: curl http://localhost:8000/ask -X POST -H "Content-Type: application/json" -d '{"question": "What is Docker?"}'
# Image size: docker images my-agent:develop → khoảng ~1GB (vì dùng python:3.11 full)
```

### Exercise 2.3: Multi-stage build (`02-docker/production/Dockerfile`)

- **Stage 1 (builder):** Cài đặt dependencies. Dùng `python:3.11-slim` + `gcc` để compile. Install vào `/root/.local` với `--user` flag.
- **Stage 2 (runtime):** Chỉ copy `site-packages` từ builder + source code. Tạo non-root user `appuser`. Image chỉ ~200MB.
- **Tại sao image nhỏ hơn?** Vì runtime image không có gcc, build tools, pip cache. Chỉ có Python runtime + packages đã cài.

**So sánh size:**
```
my-agent:develop    ~1 GB   (single-stage, python:3.11 full)
my-agent:advanced   ~200 MB (multi-stage, python:3.11-slim, non-root)
```

### Exercise 2.4: Docker Compose stack (`02-docker/production/docker-compose.yml`)

**Architecture:**
```
Client → Nginx (port 80/443) → Agent (port 8000) → Redis + Qdrant
```

**Services:**
1. **agent** — FastAPI AI agent (2 replicas), depends on redis + qdrant
2. **redis** — Session cache & rate limiting, 256MB max memory
3. **qdrant** — Vector database cho RAG
4. **nginx** — Reverse proxy, load balancer

**Communication:** Tất cả services trong network `internal` (bridge). Agent gọi redis qua `redis://redis:6379/0`, qdrant qua `http://qdrant:6333`.

### Checkpoint 2

- [x] Hiểu cấu trúc Dockerfile — FROM, WORKDIR, COPY, RUN, EXPOSE, CMD
- [x] Biết lợi ích của multi-stage builds — image nhỏ hơn, secure hơn (non-root), không có build tools
- [x] Hiểu Docker Compose orchestration — define nhiều services, networking, volumes, healthchecks
- [x] Biết cách debug container — `docker logs <id>`, `docker exec -it <id> /bin/sh`

---

## Part 3: Cloud Deployment

### Exercise 3.1: Deploy Railway

```bash
cd 03-cloud-deployment/railway
npm i -g @railway/cli
railway login
railway init
railway variables set PORT=8000
railway variables set AGENT_API_KEY=my-secret-key
railway up
railway domain
# → https://your-agent.railway.app
```

**Test:**
```bash
curl https://your-agent.railway.app/health
curl https://your-agent.railway.app/ask -X POST -H "Content-Type: application/json" -d '{"question": "Hello"}'
```

### Exercise 3.2: Deploy Render

So sánh `render.yaml` vs `railway.toml`:

| Feature | `railway.toml` | `render.yaml` |
|---------|----------------|---------------|
| Format | TOML | YAML |
| Builder | `builder = "DOCKERFILE"` | `runtime: docker` |
| Start command | `startCommand = "uvicorn ..."` | Render tự detect từ Dockerfile |
| Health check | `healthcheckPath = "/health"` | `healthCheckPath: /health` |
| Env vars | Set qua CLI (`railway variables set`) | Define trong file + dashboard |
| Auto deploy | Mặc định | `autoDeploy: true` |
| Region | Chọn trong dashboard | `region: singapore` |

### Checkpoint 3

- [x] Deploy thành công lên ít nhất 1 platform
- [x] Có public URL hoạt động
- [x] Hiểu cách set environment variables trên cloud — Railway: CLI, Render: dashboard hoặc render.yaml
- [x] Biết cách xem logs — Railway: `railway logs`, Render: dashboard → Logs tab

---

## Part 4: API Security

### Exercise 4.1: API Key authentication (`04-api-gateway/develop/app.py`)

- **API key check ở đâu?** Dependency `verify_api_key()` (line 39-54), inject vào endpoint qua `Depends(verify_api_key)`
- **Điều gì xảy ra nếu sai key?**
  - Không có key → 401 "Missing API key"
  - Sai key → 403 "Invalid API key"
- **Làm sao rotate key?** Thay đổi env var `AGENT_API_KEY` và restart app. Không cần sửa code.

### Exercise 4.2: JWT authentication (`04-api-gateway/production/auth.py`)

**JWT Flow:**
1. Client gửi `POST /auth/token` với username/password
2. Server verify credentials → tạo JWT token (payload: sub, role, iat, exp)
3. Client dùng token trong header `Authorization: Bearer <token>`
4. Server decode JWT, verify signature + expiry → extract user info

**Lợi ích vs API Key:**
- JWT chứa user info (role) → không cần query DB
- JWT có expiry → tự động hết hạn
- JWT stateless → không cần store session

### Exercise 4.3: Rate limiting (`04-api-gateway/production/rate_limiter.py`)

- **Algorithm:** Sliding Window Counter — dùng `deque` lưu timestamps, loại bỏ timestamps cũ khi check
- **Limit:** User tier: 10 req/phút, Admin tier: 100 req/phút
- **Bypass cho admin:** Dùng `rate_limiter_admin` instance riêng với `max_requests=100`

**Khi hit limit:**
```json
{
  "status_code": 429,
  "detail": {
    "error": "Rate limit exceeded",
    "limit": 10,
    "window_seconds": 60,
    "retry_after_seconds": 45
  }
}
```

### Exercise 4.4: Cost guard implementation

```python
import redis
from datetime import datetime

r = redis.Redis()

def check_budget(user_id: str, estimated_cost: float) -> bool:
    month_key = datetime.now().strftime("%Y-%m")
    key = f"budget:{user_id}:{month_key}"
    
    current = float(r.get(key) or 0)
    if current + estimated_cost > 10:
        return False
    
    r.incrbyfloat(key, estimated_cost)
    r.expire(key, 32 * 24 * 3600)  # 32 days TTL
    return True
```

**Logic:**
- Mỗi user có budget $10/tháng
- Key format: `budget:{user_id}:{YYYY-MM}` → tự reset đầu tháng
- Dùng `INCRBYFLOAT` để atomic update
- TTL 32 days → auto cleanup keys cũ

### Checkpoint 4

- [x] Implement API key authentication — `APIKeyHeader` + `verify_api_key()` dependency
- [x] Hiểu JWT flow — login → get token → use token in header → server verify
- [x] Implement rate limiting — Sliding window với deque, 429 khi exceed
- [x] Implement cost guard với Redis — track spending per user per month, block khi vượt budget

---

## Part 5: Scaling & Reliability

### Exercise 5.1: Health checks (`05-scaling-reliability/develop/app.py`)

**Implementation:**

```python
@app.get("/health")
def health():
    """Liveness probe — container còn sống không?"""
    return {
        "status": "ok",
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "version": "1.0.0",
        "checks": {"memory": {"status": "ok"}},
    }

@app.get("/ready")
def ready():
    """Readiness probe — sẵn sàng nhận traffic không?"""
    if not _is_ready:
        raise HTTPException(status_code=503, detail="Agent not ready")
    return {"ready": True, "in_flight_requests": _in_flight_requests}
```

**Phân biệt:**
- `/health` (Liveness): "Process còn sống không?" → Platform restart nếu fail
- `/ready` (Readiness): "Sẵn sàng nhận request chưa?" → Load balancer stop routing nếu fail

### Exercise 5.2: Graceful shutdown

```python
def shutdown_handler(signum, frame):
    """Handle SIGTERM từ container orchestrator"""
    logger.info(f"Received signal {signum}")
    # uvicorn tự handle:
    # 1. Stop accepting new requests
    # 2. Finish current requests (timeout_graceful_shutdown=30)
    # 3. Close connections
    # 4. Exit

signal.signal(signal.SIGTERM, shutdown_handler)
signal.signal(signal.SIGINT, shutdown_handler)
```

**Test:** Gửi request + kill cùng lúc → request vẫn hoàn thành nhờ `timeout_graceful_shutdown=30`.

### Exercise 5.3: Stateless design

**Anti-pattern (stateful):**
```python
conversation_history = {}  # ← mỗi instance có memory riêng → BUG khi scale
```

**Correct (stateless):**
```python
# Lưu vào Redis → mọi instance đều đọc được
r.set(f"session:{session_id}", json.dumps(data), ex=3600)
history = json.loads(r.get(f"session:{session_id}") or "{}")
```

**Tại sao?** Khi scale ra 3 instances, mỗi instance có memory riêng. User gửi request 1 → Instance 1 lưu session. Request 2 → Instance 2 → không có session! Redis giải quyết vì mọi instance share cùng data.

### Exercise 5.4: Load balancing

```bash
docker compose up --scale agent=3
```

**Architecture:**
```
Client → Nginx (round-robin) → Agent 1 / Agent 2 / Agent 3 → Redis
```

**Quan sát:**
- 3 agent instances start
- Nginx phân tán requests round-robin
- Response header `X-Served-By` cho thấy instance nào serve
- Nếu 1 instance die, `proxy_next_upstream error timeout http_503` → chuyển sang instance khác

### Checkpoint 5

- [x] Implement health và readiness checks — `/health` cho liveness, `/ready` cho readiness
- [x] Implement graceful shutdown — SIGTERM handler + `timeout_graceful_shutdown=30`
- [x] Refactor code thành stateless — state trong Redis, không trong memory
- [x] Hiểu load balancing với Nginx — round-robin, `proxy_next_upstream` cho failover
- [x] Test stateless design — `test_stateless.py` chứng minh session survive qua các instances khác nhau

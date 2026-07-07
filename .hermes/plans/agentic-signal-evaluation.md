# Plan: Agentic Signal Evaluation (agent)

**Created:** 2026-07-07
**Goal:** POST /api/signal/evaluate-agent — iki yönlü, session tabanlı, agentic data-fetch akışı

## Architecture

```
Matriks IQ → POST /evaluate-agent (sessionId? + market data)
              │
              ├── session yoksa → yeni session oluştur
              ├── session varsa → context'e ek veriyi ekle
              │
              ├── Planner → yeterli veri var mı?
              │   ├── EVET → AI.decide() → RiskEngine → BUY/SELL/WAIT
              │   └── HAYIR → FETCH_DATA { targetSymbol, requiredDataType }
              │
              └── Response (BUY/SELL/WAIT veya FETCH_DATA)
```

## TASKS

### 1. MODELS — `app/models/signal.py`'ye eklemeler
- [x] `DataRequestType` enum (INTRADAY_OHLC, VOLUME_DISTRIBUTION, ORDER_FLOW, NEWS_DETAIL, FUND_FLOW)
- [x] `AgentAction` enum (BUY, SELL, WAIT, FETCH_DATA)
- [x] `AgentSignalResponse` — SignalResponse'un tüm alanları + optional `fetchData`
- [x] `AgentSessionStore` dataclass — session storage

### 2. SESSION MANAGER — `app/services/agent_session.py` (yeni)
- [x] In-memory dict → TTL=300s (5 dk), maxToolCalls=3
- [x] `get_or_create_session(session_id) → AgentSession`
- [x] `add_data(session, data) → updated session`
- [x] `increment_tool_calls(session) → bool` (max 3 check)
- [x] `is_expired(session) → bool`
- [x] `cleanup_expired()` — periodic cleanup

### 3. PLANNER — `app/services/agent_planner.py` (yeni)
- [x] `plan_next_action(session) → PlanResult`
- [x] Heuristic: hangi veri tipi eksikse onu iste
- [x] maxToolCalls aşıldıysa → mevcut veriyle AI'ye git
- [x] İstenen veri tipi session'a eklenmişse → AI'ye geç

### 4. ROUTER — `app/routers/signal.py`'ye endpoint ekle
- [x] `POST /api/signal/evaluate-agent`
- [x] Session yönetimi
- [x] Mevcut `_build_payload` + `_dict_to_risk_decision` + `RiskEngine` kullan
- [x] `action=FETCH_DATA` ise `allowOrder=false`, trade logging yok
- [x] Session TTL dolduysa → WAIT
- [x] AI invalid → WAIT fallback

### 5. TESTS — `tests/test_agent_signal.py` (yeni)
- [x] Yeni oturum → FETCH_DATA döner
- [x] Veri gönderilince → ikinci turda BUY/SELL/WAIT
- [x] maxToolCalls=3 aşılınca → mecburen AI'ye gider
- [x] Session TTL dolunca → WAIT
- [x] FETCH_DATA'da allowOrder=false
- [x] Tanınmayan action → WAIT fallback
- [x] PAPER mode korunuyor
- [x] Gerçek emir gönderme kodu yok
- [x] Mevcut /api/signal/evaluate bozulmadı

### 6. DOKÜMANTASYON
- [x] README'ye agent endpoint örneği
- [x] docs/matriks-sample-request.md'ye agent flow description

### 7. CLEANUP
- [x] ruff format + lint
- [x] pytest hepsi geçiyor
- [x] docker compose build çalışıyor

## Files to create
- `app/services/agent_session.py`
- `app/services/agent_planner.py`
- `tests/test_agent_signal.py`

## Files to modify
- `app/models/signal.py` — yeni enum ve modeller
- `app/routers/signal.py` — yeni endpoint
- `README.md` — agent akışı örneği
- `docs/matriks-sample-request.md` — agent flow

## Safety guarantees
- FETCH_DATA → allowOrder=false her zaman
- targetSymbol sadece allowedSymbols içinde
- requiredDataType enum (bilinmeyen tip → WAIT)
- maxToolCallsPerSession=3
- session TTL=300s
- AI invalid → WAIT
- Default PAPER mode

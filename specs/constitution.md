# Project Constitution — Gemini Sommelier Bot

These are the stable principles every spec, plan, and implementation in this
repo must honor. They are the rules the AI agent is bound by. Changing a
principle is a deliberate act (its own spec), not a casual edit.

## 1. Minimal runtime
Python standard library + `google-genai` only. Native WSGI — no FastAPI, Flask,
or other web/routing frameworks; no pandas. Protect Vercel cold-start latency.

## 2. Serverless & stateless
Zero in-memory state between requests. Any conversation state is externalized to
the Apps Script KV store (every invocation is a cold start).

## 3. One data boundary
All cellar reads/writes go through `cellar.CellarBackend` → the single Apps
Script Web App. No second auth path. **Never** write spreadsheet columns O/P/Q;
locate the status column by header name, never by assumed position.

## 4. Fail-closed, single-user
`TELEGRAM_SECRET_TOKEN` is required (reject the request if it is unset).
`ALLOWED_USER_ID` gates access. The webhook always returns HTTP 200, even on
internal failure, so Telegram does not retry-storm.

## 5. Resilient AI
Use the model fallback chain + exponential backoff. Degrade gracefully; an AI
error must never crash the flow or leave the user without a reply.

## 6. Orchestrator-ready handlers
Every feature exposes a callable entry point usable by BOTH a slash-command and
the future intent router. No feature may be reachable only by typing its
command. (We defer a shared base class until the router exists, but the callable
seam is mandatory from day one.)

## 7. Hebrew-first UX
Friendly Israeli tone (בגובה העיניים, זורם, לא מליצי). **No em dashes** in any
user-facing text or code.

## 8. Test discipline
Pure functions + fakes cover logic before merge (the suite must stay green).
External contracts (Apps Script, Telegram) get a live smoke test before any
feature relies on them.

## 9. Human-in-the-loop gates
Work moves spec → plan → tasks → implement, and each artifact is approved before
the next is written. No skipping ahead.

## 10. Spec is the source of truth
Code conforms to the approved spec. If reality forces a change, update the spec
first, then the code — never let them silently diverge.

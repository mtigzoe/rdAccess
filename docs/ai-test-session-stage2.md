# Stage 2 local AI test-session security boundary

This PR introduces internal, read-only status and request-policy helpers.
**It does not expose an endpoint or automate NVDA Remote Access connection.**

## Current behavior

- `RDGlobalPlugin.getLocalTestStatus()` returns a sanitized internal snapshot
  from the currently attached Remote Access semantic-braille bridge.
- `localTestRequest.validateRequest()` accepts only the exact dictionary
  `{"version": 1, "action": "status"}`. It rejects connection, keyboard,
  credential, and unexpected fields.
- Neither helper writes a file, opens a socket or named pipe, stores a secret,
  launches a process, or sends a keystroke.
- A running NVDA process and an attached bridge do not prove an active remote
  connection, speech presentation, or physical braille output.

## Required before any external IPC is implemented

1. Select a **Windows-only, local**, same-user authenticated endpoint.
   Explicitly prove that another Windows account and network users cannot
   access it. A request validator is not authentication.
2. Make the endpoint **off by default**, with an accessible NVDA-controlled
   opt-in and explicit per-session approval. Ensure secure-desktop behavior
   fails closed.
3. Authenticate and authorize each operation, not just connection setup.
   Bound request sizes, processing duration, rate, and response contents.
4. Dispatch NVDA operations to its main thread. Protect against stale
   transports, disconnection, profile changes, and plugin termination.
5. For eventual remote connection, reuse the add-on's *existing* saved
   connection through an explicitly supported API. Do not accept relay
   credentials from the AI client or expose secrets in replies or logs.
6. Support cancellation and cleanup without disconnecting or restarting an
   NVDA session owned by the user. Never implement unguarded keyboard injection.
7. Verify behavior in Windows CI and separate consented live testing before
   enabling automatic connection. Preserve existing manual workflows.

The Linux launcher in PR #75 can already launch Windows NVDA on request and
start the Linux side with explicit authorization. The missing Windows add-on
auto-connect remains separately gated, and stage 3 remains a human-observed
acceptance matrix until real instrumentation exists.

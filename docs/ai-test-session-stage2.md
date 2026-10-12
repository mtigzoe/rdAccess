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

## Inactive authenticated IPC transport primitives

`localTestIpc.py` now contains an **unactivated** Windows named-pipe
prototype: bounded JSON request/response encoding, a status-only client,
and a one-request server. The server is **not imported/started by the NVDA
global plugin**. No executable test-session endpoint exists yet.

`multiprocessing.connection` authenticates possession of an ephemeral
per-session auth key at pipe connection time. This does not itself prove
same-user isolation: the Windows named-pipe ACL still needs a separately
verified security descriptor, and no token can be written to the repository,
logs, subprocess arguments, or an unsecured file. A random-looking pipe name
alone is not an authorization boundary.

Before activating the listener: enforce per-user ACLs, create accessible
NVDA-side opt-in/stop controls, integrate the ephemeral consent gate, marshal
status callbacks safely to NVDA's event thread, add request timeouts and
shutdown behavior, and write Windows integration tests. Automatic connect
and key injection remain explicitly out of scope.

### Timeout scope

The inactive IPC primitives now require the accepted connection to produce
its request, and the client to receive its response, within five seconds.
These `poll()` checks **do not** bound `Listener.accept()`, initial client
pipe connection, or a slow NVDA status callback. The listener must remain
disabled until a cancellable lifecycle and Windows same-user pipe ACL have
been implemented and tested; these receive timeouts alone are insufficient.

### Status callback timeout limitation

A read-only, thread-safe status callback can now be awaited for at most five
seconds. The IPC request fails on timeout, but the daemon callback thread
**is not cancelled** and might continue running afterward. This helper is
**not safe for NVDA APIs that require the main event thread**, and it must
not be used for keyboard actions or connecting Remote Access. A proper NVDA
main-thread dispatcher with lifecycle cancellation is still required before
live activation. Listener acceptance and Windows pipe ACL requirements are
also unresolved.

### Cooperative cancellation of status waits

The internal `waitForStatus()` helper now accepts an optional cancellation
event. An already-revoked request does not start a callback, and a pending
wait can return early when the event is set. **Cancellation does not kill the
status callback worker or close a blocked named-pipe `accept()` call.** This
is still a component-level primitive, not an activated NVDA session listener.

### Session-to-waiter revocation signal

`LocalStatusSession.cancellationEvent()` now returns an event associated with
one active consent session. Passing this event to the inactive
`waitForStatus(..., cancelled=event)` primitive allows `stop()` or credential
rotation to interrupt a status *wait*. Once replaced, an old session's event
stays signalled and does not affect the newly created session. Expiration also
signals the event when `active()`, `descriptor()`, or `cancellationEvent()`
checks discover expiry; no background expiration scheduler is installed.
This still **cannot interrupt a blocked named-pipe accept or stop a callback
thread**, and it does not enable a live listener.

### Concurrent session lifecycle

`LocalStatusSession` now serializes credential creation, rotation, consent
revocation, and reads of the active descriptor under one reentrant lock.
This prevents concurrent launcher requests from observing a partially
rotated in-memory session. Tests exercise concurrent `begin()` and
`stop()` calls. This only protects the inactive session controller;
it does not enforce Windows pipe ACLs or implement listener cancellation.

### Stale descriptor rejection

`LocalStatusSession.isCurrent(descriptor)` now checks descriptor **identity**
against the still-active session under the same lifecycle lock. A descriptor
obtained before stop, rotation, or expiration is rejected. This check must be
performed again when a future request is processed; it does not replace
transport authentication or Windows ACL enforcement.

### Invalid consent rotation

The inactive session controller now validates requested consent duration
before replacing an existing session. An invalid duration (including booleans,
floats, or values outside 1–300 seconds) raises an error without revoking
the previously authorized session. This change does not activate a listener.

### Failed credential rotation

Session credential allocation now occurs before old credentials are revoked.
If generating a replacement pipe identifier or auth key fails, the previously
active, consented session remains valid and its cancellation event remains
unset. This does not activate an IPC listener or alter Windows processes.

### Consent token allocation failure

Credential rotation now prepares both the new pipe authentication key and
its separate consent secret before revoking the existing session. Failure to
generate either secret leaves the previously authorized session intact.
The live IPC listener remains disabled; no Windows services or NVDA sessions
are altered by these changes.

### Revocation-aware read-only status dispatch

The inactive session controller now offers `readStatus(descriptor, callback)`.
It checks the descriptor before calling the bounded status helper and checks
it again before returning results. Stopping or rotating a session therefore
prevents an already completed callback from returning status to an obsolete
session. This is strictly an in-process helper: it does not start a named-pipe
listener, grant Windows access, or connect NVDA Remote Access. The callback
worker can still continue after a timeout; callers must provide only safe,
thread-independent, read-only callbacks.

### Completion-time revocation check

The bounded status callback helper now checks its cancellation event once
more after callback completion, before returning a result. This closes a
small race where revocation occurred after the wait loop observed completion.
The session controller independently rechecks descriptor identity before
returning status. Neither change activates the Windows named-pipe listener.

### Optional revocation event on one-shot status transport

The inactive `serveOneStatus()` transport now accepts an optional cancellation
event. A cancelled callback does not generate a status response; revocation is
checked again immediately before sending a response. This does **not**
interrupt a blocking Windows named-pipe `accept()` or replace a same-user ACL,
and no IPC listener is enabled by the NVDA global plugin.

### Session-bound one-shot dispatch (inactive)

`LocalStatusSession.serveStatusOnce()` now forwards only the current session's
in-memory pipe address, authentication key and cancellation event to the
one-shot status primitive. Stale descriptors are rejected before dispatch.
This method is **not connected** to the NVDA plugin and must not be activated
until the blocking accept and same-user ACL requirements have been addressed.

### Active consent-expiry cancellation

A daemon expiry timer now revokes an idle session when its consent lifetime
elapses, without requiring a client to poll status. Stopping or rotating the
session cancels the prior timer, and an old timer callback cannot revoke a
replacement session. This does not interrupt the underlying blocking Windows
named-pipe `accept()`; the transport is still inactive pending secure ACLs and
cancellable listener integration.

### NVDA global-plugin lifecycle ownership

The NVDA global plugin now creates an **inactive** `LocalStatusSession` on
initialization (including secure-desktop startup) and revokes it on plugin
termination. This provides one future cleanup boundary without opening any
pipe, starting consent, exposing credentials, or connecting to Linux. The
securely cancellable pipe listener and accessible user authorization workflow
remain prerequisites before activating test-session IPC.

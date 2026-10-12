# Stage 2 authorized local test sessions

This branch implements an opt-in Windows endpoint. Importing the add-on does
not start IPC or grant consent. The development build has not been installed
into the user's running NVDA or validated against live Orca/physical braille.

## Accessible authorization

NVDA Tools contains **Authorize Linux test session…** and **Revoke Linux test
session**. The consent dialog uses ordinary labelled wx controls and keyboard
buttons. **Allow connection using saved Remote Access relay settings** is
unchecked by default. Confirming authorizes five minutes of same-user status
queries, plus saved-connection requests only when that box is checked.

Both commands are also assignable in NVDA Input Gestures under RDAccess. No
default gesture changes existing Remote Access or braille shortcuts.

Consent expires automatically and is revoked on profile changes, lock,
secure-desktop transitions, and plugin termination. Revoke immediately marks
credentials invalid; cleanup runs on a worker without joining on NVDA's GUI
thread. The consent dialog explains that test-owned connections end with
authorization, while pre-existing connections remain intact.

## Windows transport and credential boundary

`localTestWindows.py` privately binds Win32 APIs. It creates a byte-mode pipe
with overlapped I/O, `FILE_FLAG_FIRST_PIPE_INSTANCE`, and
`PIPE_REJECT_REMOTE_CLIENTS`. The protected DACL denies network logons and
grants exactly the process TokenUser SID explicit read/write rights, excluding
`FILE_CREATE_PIPE_INSTANCE`. Handles are not inheritable. There is no insecure
default-ACL, TCP, multiprocessing/pickle, or non-Windows fallback.

Connect, read, write, acknowledgement and callback waits have deadlines and
check cancellation. Idle accept observes cancellation within a 25 ms polling
interval. `CancelIoEx` cancels pending operations; completion is reaped before
releasing OVERLAPPED buffers. This kernel completion wait is worker-only.
Requests have an eight-second exchange deadline and main-thread dispatch has
a five-second limit. A single worker serializes clients and limits accepted
operations to ten per second. Clients that connect and then stop sending are
bounded too. Response draining uses an acknowledgement rather than the
blocking `FlushFileBuffers` API.

The endpoint name and a random 32-byte authentication key rotate with each
consent. Every operation and response is HMAC-SHA256 authenticated, binding
fresh server/client nonces and a direction-specific prefix. Stale challenges,
tampered actions, wrong keys and reflected responses fail before dispatch.
Request frames are limited to 1024 bytes; responses to 4096 bytes. JSON
rejects duplicate fields, nonfinite constants, and nesting beyond eight levels.

Discovery uses `%LOCALAPPDATA%\rdaccess-test-session.bin`, containing only
user-scoped DPAPI ciphertext. The file is created under a protected DACL with
exactly one user allow ACE and a network deny ACE, then atomically published.
Only the ephemeral pipe key is in that encrypted descriptor; the saved relay
key is never exported. The worker removes its own descriptor on expiry, stop,
or failure. An NVDA crash can leave ciphertext behind, but the absent pipe
and expired credentials cannot authorize requests. Same-account programs are
inside this consent boundary; it does not isolate mutually hostile programs
running as the same Windows user, or a system administrator.

The wire operations are exactly `{"version":1,"action":"status"}`,
`connect`, and `cancel`. Extra fields, relay passwords, host overrides,
screen contents, clipboard, keystrokes and braille text are rejected. Responses
contain only a fixed result enum and the allowlisted status schema. Physical
braille remains `unverified`.

## NVDA connection management

`localTestService.py` authenticates before queuing work with `wx.CallAfter`.
The queued function rechecks current descriptor identity and secure-desktop/
lifecycle permissions. Timed-out or revoked queued work does not run.

`localTestRemote.py` uses NVDA's built-in `_remoteClient` connection API and
its actual saved `controlServer` configuration. Only an existing relay in
**Control another computer** mode is accepted. It does not enable disabled
Remote Access, accept AI-supplied relay settings, start a relay, use controlled
mode, bypass certificate checks, or enable keyboard control. Existing pending,
reconnecting, follower and local-server sessions prevent a duplicate connect.

The adapter tracks leader-session identity. A 30-second main-thread watchdog
cleans up failed or disconnected test-owned sessions. Revocation closes only
the captured owned transport, on a worker, then rechecks identity before
calling the core leader-disconnect API on the main thread. Socket shutdown
unblocks core's queue drain. It never disconnects a replacement user session.
Cancellation detaches the owned transport's core connection/certificate/closing
callbacks. Core's decorated closing-state implementation runs immediately on
the main thread while the owned session is still current. A daemon cleanup
worker retains that old session until its original connector exits and closes
any socket allocated after cancellation. Windows DNS cannot be forcibly killed;
that wait stays off the GUI thread and no NVDA shutdown path joins it.
Upstream transport construction logs its channel; a retained logger filter is
installed before connect to redact the saved relay key in delayed log records.

The legacy NVDARemote API has been inspected and is recognized for existing
connection status/reuse. New legacy automatic connections fail closed because
its cleanup invokes an inline keyboard-hook join. Its existing manual behavior
and the semantic bridge's legacy discovery remain unchanged. TeleNVDA and
other unknown connection APIs are not guessed.

## Companion launcher

Companion PR #75's branch implements:

```powershell
python .\linux_rdaccess_test_session.py start-all
```

After NVDA consent, this validates encrypted credentials, performs read-only
SSH readiness checks, then connects/reuses Windows Remote Access. The current
Linux `connect` API normally restarts Orca, so `start-all` deliberately reuses
the configured running/reconnecting Orca session. Incomplete Linux setup is a
reported prerequisite, not an implicit restart. SSH requires a verified host
key and noninteractive authentication. Failures or interruption request cleanup
of only a Windows connection created under this consent. `nvda-status` reads
status; `cancel` cancels a test-owned connection. Revoke in NVDA ends IPC itself.

The companion vendors the same four dependency-free IPC modules, with shared
wire rules. JSON CLI output never includes credentials, endpoint names, raw
exceptions, screen contents or braille text.

## Validation and remaining acceptance

Windows tests exercise real pipes, protected kernel DACLs, denial with a
restricted unrelated-identity token, DPAPI, first-instance collisions, wrong
keys, oversized frames, repeated requests, response draining, idle expiry and
revocation, and a client that never sends a request. Isolated connection tests
cover saved settings, user-session preservation, partial failure, connection
timeout, replacement races and log redaction. Existing speech/protocol/braille
tests and packaging checks must also pass.

An unrelated restricted token is a kernel security regression, not a live
second-account/network acceptance run. Before release, separately authorize
and verify the NVDA dialog with keyboard/screen-reader use, install/reload the
built add-on, test a real relay and configured Orca session, revoke/expire while
connected, check lock/profile/shutdown, and confirm speech, braille panning/
routing, and physical display output. Automated kernel tests and mocks do not
prove those outcomes. Do not merge either PR until review and acceptance.

Win32 references: [pipe security and rights](https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-security-and-access-rights),
[overlapped pipe I/O](https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-server-using-overlapped-i-o).

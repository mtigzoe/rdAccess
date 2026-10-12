# PR #10 implementation checklist

- [x] Fetch authoritative PR branches and inspect changes and CI.
- [x] Read the stage 2 design and run baseline tests.
- [x] Replace blocking multiprocessing IPC with cancellable Windows overlapped I/O.
- [x] Enforce protected same-user ACLs, remote rejection, and ephemeral authentication.
- [x] Provide protected credential discovery, expiry, revocation, and bounded cleanup.
- [x] Add accessible, explicit NVDA authorization and stop controls.
- [x] Connect through built-in NVDA Remote Access using saved settings.
- [x] Preserve existing connections and clean up only session-owned connections.
- [x] Coordinate a safe start-all launcher with companion PR #75.
- [x] Add Windows transport, security, lifecycle, and orchestration regressions.
- [x] Run lint, types, unit/integration tests, and packaging checks.
- [x] Commit and push both coherent PR updates.
- [x] Verify final Windows Actions after the pending-DNS and POT extraction corrections.
- [x] Obtain authorization for live testing and install development version 20261012.0.1.
- [x] Correct stale incremental package metadata and exclude Python bytecode.
- [ ] Resolve live consent activation and configure saved Windows controlling-relay settings.
- [ ] Complete live connection, revocation, speech and available braille acceptance.

Baseline: remote Windows head fb2287d, Linux head 12079d4. GitHub unit tests
passed; Ruff and build failed on SIM105 in localTestSession.py. Local Windows
discovery ran 408 tests with four failures in test_overlappedIo.py.

Those baseline Windows failures were sandbox access denials; all nine existing
overlapped-I/O tests pass outside the sandbox. Development package generation,
archive integrity/content checks, the real cross-repository Windows pipe/DPAPI
round trip, and the companion's 30 focused tests pass. Full repository hooks
also pass. Final counts and Actions results are recorded in the PR descriptions.

Windows CI exposed Administrator SID abbreviation in SDDL; the regression now
compares the raw ACE SID. The add-on build passed, but POT extraction rejected
the non-ASCII menu ellipsis; its source label now uses three ASCII dots.
All six companion workflows passed at 2c1045f. All three Windows workflows passed
at 14afa89 after packaging and privacy-safe diagnostic improvements: 423 unit tests,
Ruff, and add-on packaging with POT extraction.

Remaining limitations: legacy automatic connection lacks a bounded cleanup API;
start-all reuses a configured Orca session because Linux connect normally restarts
Orca. End-to-end speech, semantic presentation, physical braille, dialog accessibility,
and actual second-account/network acceptance remain unverified. Authorized live
preflight confirms Linux SSH readiness without restarting Orca. Consent activation
is unresolved: no descriptor/pipe or consent callback log appears after the reported
dialog completion. Installed source/bytecode, debug logging, and matching Windows
account/session have been verified; saved Windows relay host/key settings are absent.

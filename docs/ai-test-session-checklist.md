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
- [ ] Commit and push both coherent PR updates and verify Actions.
- [ ] Obtain separate authorization for live NVDA/Orca and physical braille testing.

Baseline: remote Windows head fb2287d, Linux head 12079d4. GitHub unit tests
passed; Ruff and build failed on SIM105 in localTestSession.py. Local Windows
discovery ran 408 tests with four failures in test_overlappedIo.py.

Those baseline Windows failures were sandbox access denials; all nine existing
overlapped-I/O tests pass outside the sandbox. Development package generation,
archive integrity/content checks, the real cross-repository Windows pipe/DPAPI
round trip, and the companion's 30 focused tests pass. Full repository hooks
also pass. Final counts and Actions results are recorded in the PR descriptions.

Remaining limitations: legacy automatic connection lacks a bounded cleanup API;
start-all reuses a configured Orca session because Linux connect normally restarts
Orca. End-to-end speech, semantic presentation, physical braille, dialog accessibility,
and actual second-account/network acceptance need separately authorized live testing.

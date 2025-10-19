# Operational TODO

## Near Term

- [ ] Add automated smoke tests that exercise the connectivity checks with mocked SDK responses.
- [ ] Expose the list of verification ping targets in a configuration file to simplify environment-specific tuning.
- [ ] Document rollback procedures (VM deletion / datastore clean-up) with screenshots for the operations team.
- [ ] Add regression coverage for the “no default gateway on STG Escenario to ensure the PRD fallback rule stays intact.
- [ ] Complete workflow refactor by extracting the remaining migration phases into `CloneAndVmotionWorkflow`.
- [ ] Split refactored logic into dedicated modules (e.g., guest commands, auth/helpers, workflow entry point).
- [ ] Add unit-level coverage around `GuestCommandExecutor` and new workflow orchestrator.
- [ ] Update developer docs to describe the new module boundaries and workflow sequence.
- [x] Refresh README/CHANGELOG (EN/JA) to document gateway fallback behaviour, root-auth suppression, and RHEL 7.9 validation notes.

## Backlog

- [ ] Evaluate supporting IPv6 configuration once PRD networks begin adopting dual-stack.
- [ ] Investigate packaging an optional HTML report summarising migration results.
- [ ] Consider building a small CLI wrapper that pre-validates credentials and network reachability before running the full script.
- [ ] Explore generating typed SDK stubs or adapters to simplify future testing.
- [ ] Run end-to-end validation on additional guest OS targets (RHEL 8.x, RHEL 9.x, modern Ubuntu releases).

# Version History

## 2025-10-26 (Guest command resilience & reporting)

- Hardened guest command file downloads to retry with urllib when `requests` fails or returns non-200 responses, ensuring stdout/stderr capture succeeds.
- Added a `--source-vm` CLI option for `cloneAndVmotion.py` so scripted runs can skip the interactive VM prompt.
- Collected `[OK]`/`[WARN]`/`[ERROR]` messages into an execution summary that prints once the migration workflow finishes.
- Reconciled firewalld zone interfaces using source-derived mappings before reload, preventing orphaned assignments.
- Updated documentation to cover the new summary, CLI flag, and guest command fallback.

## 2025-10-20 (Legacy guest tooling support)

- Reworked the legacy guest network configurator to detect `ip`, `ifconfig`, and `route`, falling back to net-tools when NetworkManager is absent.
- Added detailed verification/diagnostics so failed legacy commands surface explicit warnings and the caller can reuse the exact verification command.
- Updated the migration workflow to honour the expanded return signature, abort on legacy failures, and reuse the helper-provided verification when checking configured IPs.
- Marked static route and persistence write errors as non-fatal so legacy guests keep progressing while still logging warnings.
- Captured source interface names via guest operations and renamed destination NICs to match (including udev/ifcfg updates) before applying network settings.

## 2025-10-19 (Gateway fallback & auth hardening)

- Tightened default-route inference to consider only explicit 0.0.0.0/0 entries from vSphere REST responses, preventing duplicate PRD defaults.
- Added PRD segment fallback (third octet 160/162) with clear logging when the source VM has no default gateway.
- Ensured root guest authentication is attempted once per run; subsequent commands use the sudo-capable user automatically.
- Simplified SDK verification output by removing redundant route listings and documented the behaviour (README.md).
- Validated end-to-end flow on RHEL 7.9 (NetworkManager + `nmcli`); recorded the platform in documentation.

## 2025-10-19 (Refactor)

- Introduced `GuestCommandExecutor` to encapsulate guest command execution and simplify debugging.
- Added `CloneAndVmotionWorkflow` / `WorkflowState` scaffolding so early migration phases observe SRP.
- Centralised vCenter authentication via `authenticate_vcenter` helper to remove duplicated SmartConnect calls.

## 2025-10-18

- Improved default-gateway inference to prefer vSphere route metadata and NIC subnet checks.
- Normalised DNS verification output to remove false 窶徇issing DNS窶・warnings and clearly display actual/expected sets.
- Filtered SDK route snapshots so only non-default discrepancies are reported.
- Ensured `nmcli` connections remain in autoconnect mode and prevented duplicate static-route application.
- Updated documentation (README/SETUP) to reflect the new verification behaviour and configuration options.
- Switched `InsecureRequestWarning` imports to `urllib3` to avoid deprecation warnings when using `requests`.

## 2025-10-17

- Hardened guest command error handling with clearer exit-code reasons and CLI output capture.
- Fixed newline injection when staging admin passwords and ensured nmcli fallback logic short-circuits correctly.
- Added vCenter keep-alive threading to prevent session expiry during long storage vMotion sequences.

## 2025-10-16

- Added PRD static-route ownership tracking so each route is bound to the correct NIC during migration.
- Added nmcli post-configuration validation to confirm IP/gateway/routes/DNS after vMotion.

## 2025-10-12

- Fixed locale handling and output formatting so nmcli/ping behave consistently.
- Hardened sudo fallback logic to improve resilience when rerunning key commands.
- Restructured README.md / SETUP.md to clarify setup steps and usage.

## 2025-10-11

- Organised the flow from clone ↁEIP configuration ↁEStorage vMotion.
- Added connectivity verification and rollback steps using nmcli / ping inside the guest OS.

## 2025-10-10

- Initial project setup, establishing the migration script foundation spanning source and destination vCenters.

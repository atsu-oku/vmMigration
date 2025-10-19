# Version History

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

- Organised the flow from clone → IP configuration → Storage vMotion.
- Added connectivity verification and rollback steps using nmcli / ping inside the guest OS.

## 2025-10-10

- Initial project setup, establishing the migration script foundation spanning source and destination vCenters.


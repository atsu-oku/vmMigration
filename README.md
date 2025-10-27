# vSphere Migration Automation Scripts

`cloneAndVmotion.py` automates the end-to-end workflow required to move a VM from a staging (STG) vCenter to a production (PRD) vCenter. The script clones the source VM, re-registers it on the destination environment, rebuilds NICs against PRD networks, applies the appropriate IP/DNS/route configuration inside the guest OS, and finishes with a Storage vMotion to the final PRD datastore. Guest networking is configured through `nmcli` when available, with a shell-based fallback to support legacy environments.

---

## Quick Start

> Python 3.11 or later is required. The examples below assume PowerShell on Windows and bash on macOS/Linux.

### 1. Create and activate a virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate
```

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Clone and install dependencies

```bash
git clone https://github.com/atsu-oku/vmMigration.git
cd vmMigration
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 3. Run the migration script

```bash
python cloneAndVmotion.py
```

Alternative setup flows (e.g., creating the virtual environment before cloning, using system-wide installs) are documented in `SETUP.md`.

---

## Highlights

- Covers the full lifecycle: clone -> destination vCenter registration -> NIC rebuild -> guest IP/GW/DNS/static routes -> Storage vMotion.
- Validates the applied configuration via the vSphere Automation SDK REST APIs. Falls back to `nmcli` verification when the automation APIs are unavailable.
- Reworked default-gateway detection combines source route metadata with guest inspection to correctly identify the owning NIC. When the source VM exposes no default route, the script falls back to the PRD segment rule (third octet 160/162) and labels the inference in the logs.
- Normalises DNS comparisons to stop false "missing DNS" warnings and reports only meaningful differences.
- Avoids duplicate static-route application across NICs, clears stale default routes before reconfiguration, and keeps `nmcli` connections in autoconnect mode for post-reboot resilience.
- Provides guided rollback steps (VM deletion, datastore clean-up) when a failure occurs mid-flight.
- Collects `[OK]`/`[WARN]`/`[ERROR]` markers and prints an execution summary once the workflow completes, now including every guest command with a short Japanese description.
- Tracks firewalld zones via JSON-schema-backed plans so interface, source, port, and rich-rule assignments on the destination VM mirror the source layout.
- Writes guest files using shell-only mktemp plus bash heredocs, satisfying environments that forbid Python payloads or base64 injections.

---

## Requirements

- Network reachability to both the source and destination vCenter instances; API access must be permitted.
- VMware Tools running inside the guest with Guest Operations enabled.
- `nmcli`/NetworkManager available within the guest OS (typically Linux). When absent, the script applies a legacy configuration path, though verification coverage will be reduced.
- Python 3.11+ with the dependencies listed in `requirements.txt`.

---

## Execution Flow (Summary)

1. Collect source VM information (NICs, existing routes, DNS) and infer the default gateway/NIC ownership.
2. Clone the VM into a staging datastore on the source vCenter, removing NICs to prepare for PRD configuration.
3. Register the clone with the destination vCenter and recreate NICs mapped to the PRD networks.
4. Apply guest network settings (IP/DNS/routes) using `nmcli`, or a shell fallback when `nmcli` is unavailable. Verification is performed via the vSphere SDK and/or `nmcli`.
5. Execute the final Storage vMotion to the production datastore, then print a completion summary.

During long-running operations the script keeps both vCenter sessions alive by issuing periodic keep-alive calls. The interval can be adjusted through `VSPHERE_CLONE_KEEPALIVE_SECONDS`.

---

## Usage Notes

- Run the script with `python cloneAndVmotion.py` and follow the prompts:
  - Source/destination vCenter credentials
  - Target VM name
  - Guest OS credentials (root and/or sudo-capable user)
- Each phase presents a confirmation banner; respond with `y` to proceed.
- Increase logging verbosity by exporting `VSPHERE_CLONE_LOG_LEVEL=DEBUG`.
- After a root authentication failure the workflow automatically switches to the sudo-capable account for the remainder of the run, avoiding repeated root prompts.
- The final verification step can be switched between SDK and `nmcli` by setting `REQUESTS_AVAILABLE`; when `requests` is installed, the SDK path is preferred.
- Supply the source VM name ahead of time with `python cloneAndVmotion.py --source-vm <vm-name>` when scripting or rerunning the same workload.
- Review the execution summary printed at the end; it consolidates `[OK]`/`[WARN]`/`[ERROR]` output for quick sharing.
- Command logging is automatic. Use `remember_command_description(command, text)` when adding new guest commands so the summary includes a clear description.
- Guest stdout/stderr capture uses VMware Guest Operations. If `requests` cannot download the logs, the tool automatically retries with `urllib` so diagnostics remain available.

---

## Troubleshooting

- **Connectivity checks fail**  - ensure ICMP is permitted between the guest and your validation targets. The script prints the exact ping command used.
- **Guest authentication fails**  - confirm the VMware Tools guest operations permissions for the supplied account.
- **DNS mismatch warnings**  - the log now prints the expected vs. actual sets. If both values are empty, no warning is shown; any listed discrepancy indicates the guest did not adopt the configured servers.
- **Route discrepancies**  - only non-default mismatches are listed. Default routes (`0.0.0.0/0`) are tracked via the gateway inference logic.
- **Rollback requests**  - when an error occurs after registration, the script prompts for cleanup (VM deletion, datastore file removal). Follow the guided prompts to leave the environment consistent.

---

## Tested Platforms

- RHEL 7.9 (NetworkManager + `nmcli`)  - end-to-end migration validated 2025-10-19.

Additional guest OS reports are welcome; please file issues with findings.

---

## Further Reading

- `SETUP.md`: Detailed environment preparation options (with/without virtual environments) and logging variables.
- `CHANGELOG.md`: Version history.
- `TODO.md`: Operational to-do items and follow-up tasks.
- `docs/MIGRATION_FEATURES_EN.md`: Details recent enhancements such as command logging, firewalld schema synchronisation, and shell-only guest writes.

Feel free to open issues or PRs to improve the tooling. The maintainers welcome feedback based on lab or production migrations.


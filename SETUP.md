# Setup Guide

This document complements the quick-start section in the README and expands on environment preparation, configuration, and logging options.

---

## Repository Checkout Patterns

You can prepare the environment in two common ways. Choose the one that fits your organisational policy.

### Pattern A - Clone first, then create venv

```bash
git clone https://github.com/atsu-oku/vmMigration.git
cd vmMigration
python -m venv .venv          # python3 -m venv .venv on macOS/Linux
.\.venv\Scripts\Activate      # source .venv/bin/activate on macOS/Linux
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### Pattern B - Create venv first (policy-driven)

Some environments require creating the virtual environment before cloning the repository.

```bash
python -m venv .venv
.\.venv\Scripts\Activate      # or source .venv/bin/activate
git clone https://github.com/atsu-oku/vmMigration.git
cd vmMigration
pip install -r requirements.txt
```

The effect is the same; Pattern B simply keeps the repository inside an already-activated environment.

---

## Prerequisites

- Python 3.11 or later.
- Network access to the source and destination vCenters (API + guest operations).
- VMware Tools running inside the guest with Guest Operations permission for the supplied account.
- `nmcli`/NetworkManager available in the guest OS. If absent, the script falls back to legacy shell configuration with reduced verification coverage.

---

## Dependency Installation

All Python dependencies are listed in `requirements.txt`. Install them either inside a virtual environment or, if policy requires, in the user-scoped global environment.

```bash
pip install -r requirements.txt
```

To verify the installation:

```bash
python -c "import pyVmomi, requests; print('dependencies ok')"
```

> **Avoid `sudo pip install`** whenever possible; it tends to pollute system environments. Use `--user` for user-scoped installs if you must work outside a virtual environment.

---

## Configuration (optional)

Environmental variables allow you to tune runtime behaviour:

| Variable | Purpose | Default |
| --- | --- | --- |
| `VSPHERE_CLONE_LOG_LEVEL` | Logging level (`DEBUG`, `INFO`, `WARNING`, ...) | `WARNING` |
| `VSPHERE_CLONE_KEEPALIVE_SECONDS` | Interval for vCenter keep-alive calls | `240` |

PowerShell:

```powershell
setx VSPHERE_CLONE_LOG_LEVEL DEBUG   # persistent
$env:VSPHERE_CLONE_LOG_LEVEL = "DEBUG"  # current session only
```

bash:

```bash
export VSPHERE_CLONE_LOG_LEVEL=DEBUG
```

---

## Running the Script

```bash
python cloneAndVmotion.py
```

You'll be prompted for:

- Source/destination vCenter credentials.
- Target VM name.
- Guest OS credentials (root and/or sudo-capable admin).

Each major phase displays a summary and waits for confirmation (`y` to proceed).
After the workflow completes, review the execution summary; it now lists every guest command alongside a human-readable description.

---

## Troubleshooting Tips

- **Connectivity failures**: Ensure ICMP is permitted for ping validation, or adjust the target list within the script.
- **Authentication errors**: Confirm Guest Operations permissions within VMware Tools.
- **DNS or route warnings**: Compare the logged "expected vs actual" output. The script now suppresses false positives when values match.
- **Session drops**: Lower `VSPHERE_CLONE_KEEPALIVE_SECONDS` to keep vCenter sessions alive during long-running Storage vMotion tasks.

---

## Related Documents

- `README.md` for a project overview and high-level workflow.
- `CHANGELOG.md` for release notes.
- `TODO.md` for operational follow-ups.
- `docs/MIGRATION_FEATURES_EN.md` for a narrative of the latest automation improvements.

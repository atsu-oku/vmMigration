# find_and_extract.sh Enhancements

This note documents the operator-facing behaviour of `find_and_extract.sh` after the recent tooling upgrades.  The goal is to provide a quick reference for anyone preparing staged configurations or rolling back changes during STG->PRD migrations.

## Subcommands

| Subcommand | Description |

| --- | --- |

| `scan` (default) | Original behaviour. Scans a directory tree and reports files that contain “current” and “new” platform markers.  Output logs remain under `/tmp/<user>/find_and_extract`. |

| `transform` | Rewrites STG markers to PRD equivalents.  Dry‑run by default; use `--apply` to persist the changes. |

| `rollback` | Restores files using the log emitted by a previous `transform --apply` run. Supports global or per‑file restore via repeated `--file <path>` arguments. |

Common options:

* `--dry-run` – (transform only) report prospective edits without touching the filesystem.

* `--apply` – (transform only) apply changes after a yes/no confirmation prompt.

* `--skip-backup-files` – ignore files that look like editor/backup artefacts.

* `--file <path>` – (rollback only) restrict restoration to specific files recorded in the transform log.

## Transform Mode Behaviour

* **IP/hostname normalisation** – staging ranges (`172.16.170-179.*`, etc.), hostnames ending in `s`, and tokens containing `stg` in host definitions are rewritten to their PRD forms.  Existing PRD values are left untouched.

* **HTTPD fuel config checks** – when the target path is `/var`, the script verifies that `/var/www/com/ipet-ins/<system>/fuel/app/config/newproduction` exists for each system directory and that it contains `app.php`, `config.php`, `db.php`, `email.php`, and `session.php`.  Missing directories or files are reported in the summary.

* **Config backups** – before modifying a file, a timestamped `*.bak_<yyyymmdd_hhmmss>` copy is created alongside the original.

* **Change log** – successful `--apply` runs emit `/tmp/<user>/find_and_extract/<hostname>_<timestamp>_transform.log`.  Each line records the original path, backup path, and the pre-change mode/owner/group.

## Rollback Mode

```bash

./find_and_extract.sh rollback /tmp/<user>/find_and_extract/<host>_<ts>_transform.log

./find_and_extract.sh rollback --file /etc/hosts --file /etc/profile <log>

```

* The tool restores each listed file from its recorded backup, resetting permissions and ownership when possible.

* A summary (`Restored: X / Failed: Y`) is printed on completion.  Missing backups or malformed log entries are flagged individually.

## Logs and Location

* All generated logs (`*_current_infra.log`, `*_new_infra.log`, `*_transform.log`) remain under `/tmp/<user>/find_and_extract/`.

* Use `--deletelogs` to remove the accumulated logs for the current host when they are no longer needed.

## Schema Reference

* `schemas/find_and_extract_schema.json` tracks the CLI configuration and transform result structure.  This is useful if you plan to serialise outputs or integrate the tool with additional automation.

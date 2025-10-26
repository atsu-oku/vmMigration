# vSphere Migration Automation - Functional Specification

## Overview

- **Objective**: Move a staging VM running on vSphere into the production environment and align the in-guest configuration with PRD standards.
- **Key files**: cloneAndVmotion.py, 
etwork_utils.py, helper shell script ind_and_extract_3.2.4.0.sh.
- **Target guest OS**: RHEL / CentOS family with firewalld, chrony/ntpd, yum repos, td-agent and related tooling available.

---

## End-to-End Flow

1. **Phase 0 - Pre-flight**
   - Exercise vCenter authentication against source and destination endpoints.
   - Verify the target VM exists and confirm VMware Tools status.
2. **Phase 1 - Data collection**
   - Gather NIC, IP, DNS, route, firewall, NTP, and repository information.
   - Validate that STG->PRD transformation rules apply cleanly.
3. **Phase 2 - User confirmation**
   - Present detected diffs and obtain approval to proceed.
   - (Planned) Generate an automatic diff report via ind_and_extract.
4. **Phase 3 - Clone and register**
   - Create the clone, register it, rebuild NICs, and execute Storage vMotion.
5. **Phase 4 - Guest configuration**
   - Update /etc/hosts, firewalld, NTP, repositories, proxy exports, and iptables.
6. **Phase 5 - Verification and wrap-up**
   - Run SDK-based validation and collect warnings.
   - Provide backup locations and rollback guidance.

---

## Guest Configuration Flow (_sync_prd_system_configuration)

1. **/etc/hosts**
   - Repeatedly apply 	ransform_text_to_prd until staging patterns disappear.
   - Raise a warning if STG entries remain.
   - Use the safe mktemp -> mv replacement flow and keep /etc/hosts-YYYYMMDD.bak.

2. **firewalld**
   - Pull zone XML, convert sources and rich rules to PRD ranges.
   - Remove the SSH rule from the heartbeat zone.
   - Preserve interface assignments and reattach them after conversion (pending work).
   - Apply the changes with irewall-cmd --reload.

3. **chrony / ntpd**
   - Scan files such as /etc/chrony.conf and /etc/ntp.conf.
   - Run 	ransform_text_to_prd plus regex replacements to flip 172.16.17x.* to 172.16.16x.* (pending implementation).
   - Write back after generating a backup.

4. **CentOS repositories**
   - Inspect every /etc/yum.repos.d/*.repo, comment out mirrorlist entries.
   - Force aseurl to https://vault.centos.org/centos/.
   - On TLS errors attempt CA refresh -> curl-openssl install; fall back to warning-only if repairs fail.

5. **td-agent repository (/etc/yum.repos.d/td.repo)**
   - Resolve eleasever and rch dynamically via rpm macros.
   - Probe the v4 repository with curl; downgrade to v3 when unreachable.
   - Create a backup before writing the new file.

6. **iptables**
   - Rewrite /etc/sysconfig/iptables to PRD rules.
   - Retry systemctl reload iptables (or service iptables reload) until it succeeds or exhausts attempts.

7. **Proxy settings (/etc/profile)**
   - Append PRD proxy exports (still to be implemented).
   - Source the profile and verify with env | grep -i http.
   - Emit warnings when the environment update fails.

> All writes rely on the mktemp -> mv pattern to preserve ownership and permissions. Backups live beside the source file with a -YYYYMMDD.bak suffix.

---

## Key Helpers in 
etwork_utils.py

- calculate_ip_stg_to_prd(ip): Translate staging addresses (third octet 170-179) to PRD equivalents.
- 	ransform_text_to_prd(text): Replace staging IPs, trailing host suffix s, and ipet-ins domains with PRD forms.
- determine_prd_static_routes(...): Choose PRD static routes by prioritising MNG segment NICs (third octet 161/163).
- ensure_firewall_allows_ssh(exec, source_ip): Add SSH allowance and remove it from the heartbeat zone as required.
- ensure_connection_activation(...): Make sure the nmcli connection is up, validating connectivity with ping.
- Additional helpers cover DNS extraction, SDK verification, and post-migration consistency checks.

---

## TLS Error Mitigation (_run_curl_with_tls_repairs)

1. Execute curl and classify TLS-related failures.
2. Refresh CA bundles via update-ca-trust or yum reinstall ca-certificates nss curl.
3. If problems persist, install curl-openssl or reinstall curl.
4. When all recovery steps fail, log a warning and continue the workflow.

---

## Helper Script ind_and_extract_3.2.4.0.sh

- Scans a target directory and extracts differences for **current** vs **new** infrastructure criteria.
- Outputs hit logs under /tmp/<user>/<script_name>/ grouped into stg/prod/other buckets.
- Intended as a precursor for automatic remediation once diff detection is stabilised.
- Logs include hit locations (with line numbers), rule names, and category-specific lists.

---

## Handling Success vs Warning States

- Missing target file -> skip without failing the overall run.
- Unable to back up or write -> flag [WARN], mark the specific task failed, and lower the overall success flag.
- STG artefacts remain after conversion -> raise [WARN] and request operator review.
- TLS still unreachable after repair attempts -> log [WARN] while continuing execution.
- Environment variables not applied -> [WARN] Proxy environment variables may not be active....

---

## Future Enhancements

1. **Diff report before editing** - display ind_and_extract results via Python to narrow the scope.
2. **Preview mode for automated fixes** - show the proposed diff and apply only after approval.
3. **Rollback assistance** - list available backups with ready-to-run restore commands.
4. **Port the logic to Python** - migrate the bash matching logic into a Python module and expose a unified CLI.
5. **CI/CD integration** - provide dry-runs, unit tests, and SDK-level verification in automation.

---

## ASCII Art Notes

`
 _______________________
|  VM Migration Engine  |
|-----------------------|
|  Clone + vMotion      |
|  Guest Reconfigure    |
|  Firewalld / NTP      |
|  Repo & Proxy Sync    |
|  Reports & Backups    |
|_______________________|

        /       /        /____\   <- PRD-ready VM
`

## Appendix: Main Outputs

| File | Description |
|-----------|-----------|
| /etc/hosts-YYYYMMDD.bak | Backup taken before rewriting hosts |
| /etc/firewalld/zones/*.bak | firewalld zone XML backups |
| /etc/sysconfig/iptables-YYYYMMDD.bak | iptables backup |
| /etc/profile-YYYYMMDD.bak | Proxy backup before editing |
| /etc/yum.repos.d/*.repo-YYYYMMDD.bak | CentOS repo backups |
| /etc/yum.repos.d/td.repo-YYYYMMDD.bak | td-agent repo backup |
| /tmp/<user>/find_and_extract_3.2.4.0/ | Diff analysis logs |

---

## Summary

This project automates STG->PRD VM migrations on vSphere by coordinating network, firewall, NTP, repo, and proxy configuration changes inside the guest. It keeps backups, retries TLS fixes, surfaces warnings, and leaves room for future automation around diff detection and self-healing.

`
   ________
  /  ____/ /__/ __\/ \  \ \_/\/
 \__\__\/   <- STG
   ||
   ||  (cloneAndVmotion.py)
   \/
  /\/\  ____
 / /\/ / __  \/ / / / _ |
   \/ | | (_| |
       \ \__,_|
        \____/  <- PRD
`

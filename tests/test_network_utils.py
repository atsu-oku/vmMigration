import unittest
from typing import Dict, Tuple

from network_utils import ensure_firewall_allows_ssh


class EnsureFirewallAllowsSshTests(unittest.TestCase):
    def _make_executor(self, responses, executed):
        def _executor(cmd: str, check_exit_code: bool = True) -> Tuple[int, str, str]:
            executed.append(cmd)
            if cmd in responses:
                return responses[cmd]
            return 0, "", ""
        return _executor

    def test_firewalld_adds_rule_when_missing(self) -> None:
        source_ip = "172.16.164.7"
        zone = "trusted"
        rule = f"rule family=\"ipv4\" source address=\"{source_ip}\" service name=\"ssh\" accept"
        rich_rule_cmd = (
            f"firewall-cmd --permanent --zone={zone} "
            f"--add-rich-rule='{rule}'"
        )
        responses: Dict[str, Tuple[int, str, str]] = {
            "command -v systemctl": (0, "", ""),
            "systemctl show firewalld.service --property=ActiveState": (0, "ActiveState=active", ""),
            "firewall-cmd --get-default-zone": (0, f"{zone}\n", ""),
            "firewall-cmd --get-active-zones": (0, "", ""),
            f"firewall-cmd --permanent --zone={zone} --list-rich-rules": (0, "", ""),
            f"firewall-cmd --permanent --zone={zone} --list-services": (0, "", ""),
            rich_rule_cmd: (0, "", ""),
            "firewall-cmd --reload": (0, "", ""),
        }
        executed: list[str] = []
        executor = self._make_executor(responses, executed)
        ensure_firewall_allows_ssh(executor, source_ip)
        self.assertIn(rich_rule_cmd, executed)
        self.assertIn("firewall-cmd --reload", executed)

    def test_firewalld_skips_when_service_allows_broadly(self) -> None:
        source_ip = "172.16.164.7"
        zone = "trusted"
        rule = f"rule family=\"ipv4\" source address=\"{source_ip}\" service name=\"ssh\" accept"
        rich_rule_cmd = (
            f"firewall-cmd --permanent --zone={zone} "
            f"--add-rich-rule='{rule}'"
        )
        responses = {
            "command -v systemctl": (0, "", ""),
            "systemctl show firewalld.service --property=ActiveState": (0, "ActiveState=active", ""),
            "firewall-cmd --get-default-zone": (0, f"{zone}\n", ""),
            "firewall-cmd --get-active-zones": (0, "", ""),
            f"firewall-cmd --permanent --zone={zone} --list-rich-rules": (0, "", ""),
            f"firewall-cmd --permanent --zone={zone} --list-services": (0, "ssh\n", ""),
        }
        executed: list[str] = []
        executor = self._make_executor(responses, executed)
        ensure_firewall_allows_ssh(executor, source_ip)
        self.assertNotIn(rich_rule_cmd, executed)

    def test_iptables_conflict_skips_updates(self) -> None:
        source_ip = "172.16.164.7"
        responses = {
            "command -v systemctl": (1, "", ""),
            "command -v iptables": (0, "", ""),
            "iptables -S": (0, "-A INPUT -p tcp --dport 22 -j ACCEPT\n", ""),
        }
        executed: list[str] = []
        executor = self._make_executor(responses, executed)
        ensure_firewall_allows_ssh(executor, source_ip)
        self.assertNotIn(
            f"iptables -I INPUT 1 -p tcp -s {source_ip} --dport 22 -j ACCEPT",
            executed,
        )


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
import unittest

from firewalld_manager import apply_zone_ports, apply_zone_sources, apply_service_ports


class FirewalldManagerTests(unittest.TestCase):
    def test_apply_zone_ports(self) -> None:
        executed = []

        def executor(cmd: str, check_exit_code: bool = True) -> tuple[int, str, str]:
            executed.append(cmd)
            if "--list-ports" in cmd:
                return 0, "80/tcp\n", ""
            return 0, "", ""

        apply_zone_ports("public", ["80/tcp", "443/tcp"], executor)
        self.assertIn("firewall-cmd --zone public --add-port 443/tcp --permanent", executed)

    def test_apply_zone_sources(self) -> None:
        executed = []

        def executor(cmd: str, check_exit_code: bool = True) -> tuple[int, str, str]:
            executed.append(cmd)
            if "--list-sources" in cmd:
                return 0, "172.16.163.0/24\n", ""
            return 0, "", ""

        apply_zone_sources("public", ["172.16.163.0/24", "10.0.0.0/8"], executor)
        self.assertIn("firewall-cmd --zone public --add-source 10.0.0.0/8 --permanent", executed)
        command_log = " ; ".join(executed)
        self.assertNotIn("--set-default-zone", command_log)

    def test_apply_zone_sources_preserves_link_local(self) -> None:
        executed = []

        def executor(cmd: str, check_exit_code: bool = True) -> tuple[int, str, str]:
            executed.append(cmd)
            if "--list-sources" in cmd:
                return 0, "169.254.0.0/16\n10.0.0.0/8\n", ""
            return 0, "", ""

        apply_zone_sources("trusted", ["10.0.0.0/8"], executor)
        command_log = " ; ".join(executed)
        self.assertNotIn("firewall-cmd --zone trusted --remove-source 169.254.0.0/16 --permanent", command_log)
        self.assertNotIn("--set-default-zone", command_log)

    def test_apply_service_ports(self) -> None:
        executed = []

        def executor(cmd: str, check_exit_code: bool = True) -> tuple[int, str, str]:
            executed.append(cmd)
            if "--info-service" in cmd:
                return 0, "ports: 443/tcp\n", ""
            if "--list-ports" in cmd:
                return 0, "80/tcp\n", ""
            return 0, "", ""

        apply_service_ports("public", ["https"], executor)
        self.assertIn("firewall-cmd --zone public --add-port 443/tcp --permanent", executed)


if __name__ == "__main__":
    unittest.main()

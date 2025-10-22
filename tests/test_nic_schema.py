# -*- coding: utf-8 -*-
import unittest

from nic_schema import NIC_PLAN_VALIDATOR, NicPlan


class NicSchemaTests(unittest.TestCase):
    def test_validation_accepts_required_fields(self) -> None:
        raw = {
            "index": 0,
            "network_name": "STG-DMZ-Manage-seg",
            "mac_address": "00:50:56:aa:bb:cc",
        }
        NIC_PLAN_VALIDATOR.validate(raw)
        nic = NicPlan.from_raw(raw, NIC_PLAN_VALIDATOR)
        self.assertEqual(nic.index, 0)
        self.assertEqual(nic.network_name, "STG-DMZ-Manage-seg")

    def test_validation_rejects_unknown_key(self) -> None:
        raw = {
            "index": 1,
            "network_name": "STG-DMZ-seg",
            "mac_address": "00:50:56:11:22:33",
            "unknown": 1,
        }
        with self.assertRaises(ValueError):
            NIC_PLAN_VALIDATOR.validate(raw)


if __name__ == "__main__":
    unittest.main()

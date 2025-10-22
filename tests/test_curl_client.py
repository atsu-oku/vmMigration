# -*- coding: utf-8 -*-
import subprocess
import unittest
from unittest import mock

from curl_client import CurlError, CurlResult, head, run_curl


class CurlClientTests(unittest.TestCase):
    def test_run_curl_success(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["curl", "--silent"],
            returncode=0,
            stdout="ok",
            stderr="",
        )
        with mock.patch("subprocess.run", return_value=completed) as mocked_run:
            result = run_curl(["--head", "https://example.com"])
        mocked_run.assert_called_once()
        self.assertIsInstance(result, CurlResult)
        self.assertTrue(result.succeeded)
        self.assertEqual(result.stdout, "ok")

    def test_run_curl_failure_raises(self) -> None:
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaises(CurlError):
                run_curl(["--head", "https://example.com"])

    def test_head_uses_run_curl(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["curl"],
            returncode=0,
            stdout="",
            stderr="",
        )
        with mock.patch("subprocess.run", return_value=completed) as mocked_run:
            head("https://example.com")
        self.assertTrue(mocked_run.called)


if __name__ == "__main__":
    unittest.main()

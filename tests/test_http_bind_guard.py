"""HTTP 绑定门禁契约：非回环地址必须显式 --allow-remote，默认只允许回环。

全部离线：只调用纯函数与 main() 的早退/放行分支，mcp.run 一律 mock，
不启动 FastMCP 或 uvicorn，也不触碰设备与网络。
"""
from __future__ import annotations

import importlib
import os
import sys
import unittest
from unittest import mock


def load_mcp_server():
    """导入 mcp_server 但不让它改本进程的 fd 1（导入即 dup2(2, 1)）。"""
    saved = sys.stdout
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        with mock.patch("os.dup2"), mock.patch("os.dup", return_value=devnull):
            return importlib.import_module("mcp_server")
    finally:
        sys.stdout = saved


mcp_server = load_mcp_server()


class IsLoopbackHostTest(unittest.TestCase):
    def test_accepts_loopback_spellings(self):
        for host in ("127.0.0.1", "::1", "localhost", "LOCALHOST", "LocalHost",
                     " 127.0.0.1 ", " ::1 ", "[::1]"):
            with self.subTest(host=host):
                self.assertTrue(mcp_server.is_loopback_host(host))

    def test_rejects_non_loopback(self):
        for host in ("0.0.0.0", "192.168.1.10", "10.0.0.5", "127.0.0.2",
                     "localhost.example.com", "example.com", "", "::", None):
            with self.subTest(host=host):
                self.assertFalse(mcp_server.is_loopback_host(host))


class ValidateHttpBindTest(unittest.TestCase):
    def test_loopback_passes_without_flag(self):
        for host in ("127.0.0.1", "::1", "localhost"):
            with self.subTest(host=host):
                self.assertIsNone(mcp_server.validate_http_bind(host))
                self.assertIsNone(mcp_server.validate_http_bind(host, allow_remote=True))

    def test_non_loopback_rejected_without_flag(self):
        for host in ("0.0.0.0", "192.168.1.10", "example.com"):
            with self.subTest(host=host):
                error = mcp_server.validate_http_bind(host)
                self.assertIsNotNone(error)
                self.assertIn(host, error)
                self.assertIn("--allow-remote", error)
                self.assertIn("认证", error)

    def test_non_loopback_passes_with_flag(self):
        for host in ("0.0.0.0", "192.168.1.10", "example.com"):
            with self.subTest(host=host):
                self.assertIsNone(mcp_server.validate_http_bind(host, allow_remote=True))


class MainBindGateTest(unittest.TestCase):
    def test_default_http_binds_loopback(self):
        with mock.patch.object(type(mcp_server.mcp), "run") as run:
            code = mcp_server.main(["--http", "--port", "9999"])
        self.assertEqual(code, 0)
        self.assertEqual(mcp_server.mcp.settings.host, "127.0.0.1")
        self.assertEqual(mcp_server.mcp.settings.port, 9999)
        run.assert_called_once_with(transport="streamable-http")

    def test_remote_host_without_flag_does_not_start(self):
        with mock.patch.object(type(mcp_server.mcp), "run") as run, \
             mock.patch.object(mcp_server.logger, "error") as log_error:
            code = mcp_server.main(["--http", "--host", "0.0.0.0"])
        self.assertNotEqual(code, 0)
        run.assert_not_called()
        self.assertIn("--allow-remote", log_error.call_args[0][1])

    def test_remote_host_with_flag_starts_and_warns(self):
        with mock.patch.object(type(mcp_server.mcp), "run") as run, \
             mock.patch.object(mcp_server.logger, "warning") as log_warning:
            code = mcp_server.main(
                ["--http", "--host", "0.0.0.0", "--allow-remote", "--port", "9998"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(mcp_server.mcp.settings.host, "0.0.0.0")
        run.assert_called_once_with(transport="streamable-http")
        log_warning.assert_called_once()

    def test_stdio_ignores_remote_host(self):
        with mock.patch.object(type(mcp_server.mcp), "run") as run:
            code = mcp_server.main(["--host", "0.0.0.0"])
        self.assertEqual(code, 0)
        run.assert_called_once_with(transport="stdio")


if __name__ == "__main__":
    unittest.main()

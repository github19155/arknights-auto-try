"""stdout 隔离与失败语义：库路径只写 stderr，下载失败抛 RuntimeError，MCP 结构化返回。

全部离线：缓存/网络/设备都用 mock 或临时目录替代。
"""
from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from src.planner import intel

STAGE = {
    "code": "XX-1", "stageId": "act99side_01", "cache_key": "XX-1",
    "levelId": "Activities/act99side/level_act99side_01", "name": "测试关",
    "stageType": "ACTIVITY", "apCost": 20, "difficulty": "NORMAL",
    "variants": {"act99side_01": "NORMAL"}, "story_only": False,
}


def load_mcp_server():
    """导入 mcp_server 但不让它改本进程的 fd 1（导入即 dup2(2, 1)）。

    os.dup / os.dup2 被替换：原始 stdio 不动，模块拿到的 sys.stdout 只是
    devnull 包装；导入后恢复真实 sys.stdout。
    """
    saved = sys.stdout
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        with mock.patch("os.dup2"), mock.patch("os.dup", return_value=devnull):
            return importlib.import_module("mcp_server")
    finally:
        sys.stdout = saved


class LibraryStdoutIsolationTest(unittest.TestCase):
    def test_ensure_tables_cold_cache_logs_to_stderr_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(intel, "CACHE", Path(tmp)), \
                 mock.patch.object(intel, "http_bytes", return_value=b"{}"), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                tables = intel.ensure_tables()
        self.assertEqual(set(tables), set(intel.TABLES))
        self.assertEqual(out.getvalue(), "")
        self.assertIn("[cache] downloading", err.getvalue())

    def test_fetch_references_query_failure_logs_to_stderr_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(intel, "DATA", Path(tmp)), \
                 mock.patch.object(intel, "http_json", side_effect=OSError("offline")), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                refs = intel.fetch_references(STAGE, [], take=3)
        self.assertEqual(refs, [])
        self.assertEqual(out.getvalue(), "")
        self.assertIn("reference query failed", err.getvalue())


class FetchLevelFailureTest(unittest.TestCase):
    def test_download_failure_raises_exception_not_system_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(intel, "DATA", Path(tmp)), \
                 mock.patch.object(intel, "http_bytes",
                                   side_effect=urllib.error.URLError("offline")):
                with self.assertRaises(RuntimeError) as ctx:
                    intel.fetch_level(STAGE)
        self.assertNotIsInstance(ctx.exception, SystemExit)
        self.assertIn("level download failed", str(ctx.exception))

    def test_cli_main_returns_nonzero_on_download_failure(self):
        err = io.StringIO()
        with mock.patch.object(intel, "ensure_tables", return_value={}), \
             mock.patch.object(intel, "resolve_stage", return_value=STAGE), \
             mock.patch.object(intel, "fetch_level",
                               side_effect=RuntimeError("level download failed: offline")), \
             contextlib.redirect_stderr(err):
            code = intel.main(["XX-1"])
        self.assertEqual(code, 1)
        self.assertIn("level download failed", err.getvalue())


class McpToolFailurePathTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        (self.data / "levels").mkdir()
        (self.data / "operators.json").write_text(
            json.dumps({"source": "local", "syncTime": None, "own_opers": []}),
            encoding="utf-8")
        self.tables = {"stage_table": {"stages": {"act99side_01": {}}},
                       "character_table": {}, "skill_table": {},
                       "enemy_database": {}}
        self.stage = {**STAGE, "cache_key": "XX-1"}
        profile = {"fixed_squad": False, "predefined_ops": [], "zero_sanity": False}
        for patch in [
            mock.patch.object(intel, "DATA", self.data),
            mock.patch.object(intel, "ensure_tables", return_value=self.tables),
            mock.patch.object(intel, "resolve_stage", return_value=self.stage),
            mock.patch.object(intel, "resolve_stage_profile", return_value=profile),
            mock.patch.object(intel, "_mechanics_intel", return_value=("", None, {})),
            mock.patch.object(intel, "data_version", return_value={}),
            mock.patch.object(intel, "fetch_references", return_value=[]),
        ]:
            patch.start()
            self.addCleanup(patch.stop)
        self.server = load_mcp_server()

    def test_download_failure_becomes_structured_tool_error(self):
        with mock.patch.object(intel, "http_bytes",
                               side_effect=urllib.error.URLError("offline")):
            result = self.server.get_stage_status_and_intel("XX-1")
        self.assertIsInstance(result, dict)
        self.assertFalse(result["ok"])
        self.assertIn("level download failed", result["error"])

    def test_intel_exception_is_caught_not_raised(self):
        with mock.patch.object(self.server, "_stage_intel",
                               side_effect=RuntimeError("level download failed: offline")):
            result = self.server.get_stage_status_and_intel("XX-1")
        self.assertFalse(result["ok"])
        self.assertIn("level download failed", result["error"])


if __name__ == "__main__":
    unittest.main()

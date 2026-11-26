"""run_copilot 端到端离线集成测试：假 Asst + 临时目录 + 合成截图。

覆盖三条真实产品路径：

  A. 漏怪中断 + 放弃链在「行动结果」结算页确认退出：``exit_confirmed`` 成立、
     不落 ``ABANDON_FAILED``、四宫格 PNG/sidecar 落盘，且收尾清理失败只写
     warning，不把 ``program_status`` 改判成 failed。
  B. 三星胜利：fake 回调 ``MarkBattleWon``、fake ``get_image`` 返回三枚蓝章
     的结算页，最终 ``battle_result=won`` / ``ok=True`` /
     ``win_evidence=verified``。
  C. 对照：灰章结算页（rejected）与抓帧失败（unverified）都不得判胜利。
  D. 主动停止 Copilot 引发的 10000 TaskChainError 只降级为诊断事件，不改判
     completed；未经 stop 的自然失败仍落 failed。

测试用事件回调驱动假 MAA：``running()`` 轮询推进 Copilot 链，Custom 链在
``start()`` 内同步回放，没有连设备/网络的线程；只有产品自身的采样线程按
150ms 节拍抓帧。所有目录（capture/MAA user/squad）都在临时目录，关卡画像
用不存在的 cache_key，确保不读取真实 ``data/``。
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from battle_control_fixtures import write_battle_control_templates

from src.engine import runner
from src.engine.recorder import parse_maa_events

ASSETS = Path(__file__).resolve().with_name("assets")
STAGE_CODE = "FC-1"
#: 刻意指向不存在的画像文件：让 tile_grid/life_total 都走「没有本地数据」分支。
CACHE_KEY = "ZZ-RUNNER-INTEGRATION-NO-DATA"
PROFILE = {
    "stage_code": STAGE_CODE,
    "stage_id": "main_01-07",
    "cache_key": CACHE_KEY,
    "zero_sanity": False,
    "fixed_squad": False,
}
TASK_SCRIPTS = {
    "BattleReadyCheck": {"marks": ["MarkAtPanel"]},
    "ResultFailedCheck": {"error": "未识别到关键字"},
    "ResultWinCheck": {"marks": ["MarkBattleWon"]},
    "AbandonEntry": {"marks": ["MarkAbandonActionEnded"]},
}


def png_bytes(name: str) -> bytes:
    """把 tests/assets 的合成画面编码成 MAA get_image 返回的 PNG 字节。"""
    buffer = io.BytesIO()
    with Image.open(ASSETS / f"{name}.png") as image:
        image.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


def mark_payload(chain: str, task_id: int, mark: str) -> dict:
    """真实 MAA ``ProcessTask`` 命中节点的事件形状。"""
    return {
        "class": "asst::ProcessTask",
        "details": {
            "action": "Stop",
            "algorithm": "JustReturn",
            "exec_times": 1,
            "max_times": 2147483647,
            "result": {},
            "task": mark,
        },
        "first": [chain],
        "pre_task": "",
        "subtask": "ProcessTask",
        "taskchain": chain,
        "taskid": task_id,
        "uuid": "offline-fake",
    }


def copilot_action_payload(task_id: int) -> dict:
    """``CopilotAction`` 用 -1 的 elapsed_time：runner 必须用本地战斗秒补齐。"""
    return {
        "class": "asst::BattleProcessTask",
        "details": {"action": "Deploy", "doc": "", "doc_color": "",
                    "elapsed_time": -1, "target": "米格鲁"},
        "subtask": "BattleProcessTask",
        "taskchain": "Copilot",
        "taskid": task_id,
        "uuid": "offline-fake",
        "what": "CopilotAction",
    }


class FakeMaa:
    """事件驱动的假 MAA：只回放回调与截图，不连接设备、不联网。

    - ``append_task`` 入队；``start`` 对 Custom 链同步回放（命中/未命中），
      对 Copilot 链置 ``battle_active``，由 ``running()`` 轮询推进（每轮发
      一条 CopilotAction，达到 ``copilot_polls`` 后正常完成）。
    - ``get_image`` 在战斗中按 ``battle_frames`` 顺序出帧（末帧重复），
      战斗结束/停止后返回 ``settlement_frame``。
    - ``stop`` 在 Copilot 链仍在跑时补发 10000 TaskChainError：真实 MAA 被
      主动停止时就是这样收尾的。
    """

    def __init__(self, *, battle_frames, settlement_frame, scripts=None,
                 copilot_polls=3, images_fail_after_battle=False):
        self.battle_frames = list(battle_frames)
        self.settlement_frame = settlement_frame
        self.scripts = dict(TASK_SCRIPTS if scripts is None else scripts)
        self.copilot_polls = max(1, copilot_polls)
        self.images_fail_after_battle = images_fail_after_battle
        self.callback = None
        self.pending: list[tuple[int, str, dict]] = []
        self.next_task_id = 1
        self.copilot_task_id = None
        self.loaded: list[dict] = []
        self.appended: list[tuple[str, dict]] = []
        self.connections: list[tuple[str, str, str]] = []
        self.instance_options: list[tuple[str, str]] = []
        self.starts = 0
        self.stops = 0
        self.battle_active = False
        self.polls = 0
        self.frame_cursor = 0
        self.Asst, self.InstanceOptionType = self._fake_bindings()

    # -- 绑定工厂 -------------------------------------------------------
    def _fake_bindings(self):
        controller = self

        class FakeInstanceOptionType:
            touch_type = "touch_type"

        class FakeAsst:
            # ctypes.CFUNCTYPE(回调签名) 的调用行为：原样包一层可调用对象。
            CallBackType = staticmethod(lambda function: function)

            def __init__(self, callback=None, arg=None):
                controller.callback = callback

            @staticmethod
            def load(path, incremental_path=None, user_dir=None):
                controller.loaded.append({"path": str(path),
                                          "user_dir": str(user_dir)})
                return True

            def set_instance_option(self, option_type, value):
                controller.instance_options.append((option_type, value))
                return (option_type == FakeInstanceOptionType.touch_type
                        and value == "maatouch")

            def connect(self, adb_path, address, config="General"):
                controller.connections.append(
                    (str(adb_path), str(address), str(config)))
                return True

            def get_image(self, size):
                return controller.get_image(size)

            def append_task(self, type_name, params=None):
                return controller.append_task(type_name, params or {})

            def start(self):
                return controller.start()

            def stop(self):
                return controller.stop()

            def running(self):
                return controller.running()

        return FakeAsst, FakeInstanceOptionType

    def bindings(self):
        return self.Asst, self.InstanceOptionType

    # -- 回调 -----------------------------------------------------------
    def emit(self, message: int, payload: dict) -> None:
        callback = self.callback
        if callback is None:
            return
        details = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        callback(int(message), details, None)

    def emit_chain_start(self, chain: str, task_id: int) -> None:
        self.emit(10001, {"taskchain": chain, "taskid": task_id,
                          "uuid": "offline-fake"})

    def emit_chain_done(self, chain: str, task_id: int) -> None:
        self.emit(10002, {"taskchain": chain, "taskid": task_id,
                          "uuid": "offline-fake"})

    # -- MAA 实例行为 ---------------------------------------------------
    def append_task(self, type_name: str, params: dict) -> int:
        task_id = self.next_task_id
        self.next_task_id += 1
        self.pending.append((task_id, type_name, params))
        self.appended.append((type_name, params))
        return task_id

    def start(self) -> bool:
        self.starts += 1
        if not self.pending:
            return False
        task_id, type_name, params = self.pending.pop(0)
        if type_name == "Copilot":
            self.copilot_task_id = task_id
            self.emit_chain_start("Copilot", task_id)
            self.battle_active = True
            self.polls = 0
            return True
        self._run_custom_chain(task_id, type_name, params)
        return True

    def _run_custom_chain(self, task_id: int, type_name: str, params: dict) -> None:
        names = list(params.get("task_names") or [])
        name = names[0] if names else type_name
        spec = self.scripts.get(name, {})
        chain = "Custom"
        self.emit_chain_start(chain, task_id)
        for mark in spec.get("marks", ()):
            self.emit(20001, mark_payload(chain, task_id, mark))
            self.emit(20002, mark_payload(chain, task_id, mark))
        error = spec.get("error")
        if error:
            # 真实 MAA 未识别到关键字时以 TaskChainError 结束（runner 对登记过的
            # 探测链会降级成 -3 诊断事件）。
            self.emit(10000, {"taskchain": chain, "taskid": task_id,
                              "uuid": "offline-fake", "why": error})
        else:
            self.emit_chain_done(chain, task_id)

    def running(self) -> bool:
        if not self.battle_active:
            return False
        self.polls += 1
        self.emit(20003, copilot_action_payload(self.copilot_task_id))
        if self.polls >= self.copilot_polls:
            self.emit_chain_done("Copilot", self.copilot_task_id)
            self.battle_active = False
        return self.battle_active

    def stop(self) -> bool:
        self.stops += 1
        task_id = self.copilot_task_id
        stopped_in_battle = self.battle_active and task_id is not None
        self.battle_active = False
        if stopped_in_battle:
            # 真实 MAA 被 stop 时会中断当前 Copilot 链，以 TaskChainError 收尾；
            # runner 必须把这种「主动停止」当成预期语义，而不是运行失败。
            self.emit(10000, {"taskchain": "Copilot", "taskid": task_id,
                              "uuid": "offline-fake", "why": "已主动停止"})
        return True

    def get_image(self, size: int) -> bytes:
        if self.images_fail_after_battle and not self.battle_active:
            raise RuntimeError("假 MAA：结算页抓帧失败（离线注入）")
        if self.battle_active and self.battle_frames:
            index = min(self.frame_cursor, len(self.battle_frames) - 1)
            self.frame_cursor += 1
            return self.battle_frames[index]
        return self.settlement_frame


class CleanupWarningFakeMaa(FakeMaa):
    """三星胜利后让 finally 的收尾 ``running()`` 失败一次（离线注入）。

    战斗结束后的前两次 ``running()`` 属于 ResultFailedCheck/ResultWinCheck 探测
    链；第三次只可能来自 finally 的收尾检查。失败会被 runner 压进
    ``cleanup_errors``，随后落一条 ``RunnerWarning``（而不是运行期
    ``RunnerError``），正好用来验证「警告不改判胜利」。
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.after_battle_running_calls = 0

    def running(self) -> bool:
        if self.battle_active or self.polls < self.copilot_polls:
            return super().running()
        self.after_battle_running_calls += 1
        if self.after_battle_running_calls > 2:
            raise RuntimeError("假 MAA：收尾 running() 失败（offline-injected）")
        return False


class NaturalCopilotFailureFakeMaa(FakeMaa):
    """Copilot 链自然失败：MAA 自己以 10000 结束，从未被 runner 主动 stop。"""

    def running(self) -> bool:
        if not self.battle_active:
            return False
        self.polls += 1
        self.emit(20003, copilot_action_payload(self.copilot_task_id))
        if self.polls >= self.copilot_polls:
            self.emit(10000, {"taskchain": "Copilot",
                              "taskid": self.copilot_task_id,
                              "uuid": "offline-fake",
                              "why": "战斗失败（离线注入）"})
            self.battle_active = False
        return self.battle_active


class RunnerIntegrationBase(unittest.TestCase):
    """统一装配：临时 outdir/MAA 目录、假绑定、假归位，绝不碰真实 data。"""

    def setUp(self) -> None:
        self.assertFalse(
            (runner.ROOT / "data" / "levels" / f"{CACHE_KEY}-intel.json").exists(),
            "测试 cache_key 指向了真实情报文件")
        self.assertFalse(
            (runner.ROOT / "data" / "levels" / f"{CACHE_KEY}.json").exists(),
            "测试 cache_key 指向了真实关卡文件")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.outdir = self.base / "capture"
        self.outdir.mkdir()
        self.maa_dir = self.base / "maa"
        self.maa_dir.mkdir()
        self.abandon_dir = self.base / "abandon-battle"
        self.paused_template = self.abandon_dir / "resource/template/BattlePauseMenu.png"
        write_battle_control_templates(
            self.maa_dir / "resource/template/Battle/BattleFlag/BattleOfficiallyBegin.png",
            self.paused_template,
        )
        self.maa_user = self.base / "maa-user"
        self.squad = self.base / "squad.json"
        self.squad.write_text(json.dumps(
            {"stage_name": STAGE_CODE, "actions": []}), encoding="utf-8")
        self.panel_calls: list[dict] = []

    def fake_panel(self, stage_code, **kwargs):
        self.panel_calls.append({"stage": stage_code, **kwargs})
        return {"panel": True}

    def run_offline(self, controller: FakeMaa, *, manifest_blocked=False,
                    timeout=30, panel_side_effect=None) -> dict:
        """patch 硬门禁/原生绑定/默认路径后跑一次真实的 run_copilot。

        ``panel_side_effect`` 替换默认的假归位（成功返回 panel 字典），用于注入
        归位失败；默认仍走 ``fake_panel``。
        """
        if manifest_blocked:
            # 用目录占住 review-manifest.json：清单写入必然失败，且失败发生在
            # finally 的 warning 落盘之后，只能进 cleanup_errors/cleanup_warnings。
            (self.outdir / runner.REVIEW_MANIFEST_NAME).mkdir()
        patches = [
            mock.patch.object(
                runner, "_prepare_job",
                return_value=({"stage_name": STAGE_CODE, "actions": []}, dict(PROFILE))),
            mock.patch.object(runner, "_native_bindings",
                              return_value=controller.bindings()),
            mock.patch.object(
                runner, "_ensure_stage_panel",
                side_effect=(self.fake_panel if panel_side_effect is None
                             else panel_side_effect)),
            mock.patch.object(runner, "_advance_post_battle_story",
                              side_effect=AssertionError("不得推进真实战后剧情")),
            mock.patch.object(runner, "DEFAULT_MAA", str(self.maa_dir)),
            mock.patch.object(runner, "DEFAULT_ADB", str(self.base / "adb.exe")),
            mock.patch.object(runner, "DEFAULT_SERIAL", "offline-serial"),
            mock.patch.object(runner, "ABANDON_BATTLE", self.abandon_dir),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return runner.run_copilot(
            STAGE_CODE, str(self.squad), mode="exercise",
            outdir=str(self.outdir), timeout=timeout,
            maa_user=str(self.maa_user))

    def summary(self, result: dict) -> dict:
        return json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))

    def events(self, result: dict) -> list[dict]:
        return [json.loads(line) for line in
                Path(result["events_path"]).read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def event_whats(self, result: dict) -> list[str]:
        whats = []
        for event in self.events(result):
            details = json.loads(event["details"])
            if isinstance(details, dict) and details.get("what"):
                whats.append(details["what"])
        return whats


class MissingRuntimeTemplateTest(RunnerIntegrationBase):
    def test_missing_paused_template_refuses_battle_before_device_connection(self):
        self.paused_template.unlink()
        maa = FakeMaa(battle_frames=[], settlement_frame=png_bytes("result-cleared"))
        with self.assertRaises(FileNotFoundError) as error:
            self.run_offline(maa)
        self.assertIn(str(self.paused_template), str(error.exception))
        self.assertEqual(maa.connections, [])


class LeakAbortIntegrationTest(RunnerIntegrationBase):
    """A. 漏怪 + 「行动结果」结算页：确认退出、留证据、warning 不改判。"""

    def test_leak_abort_at_action_result_stays_completed_with_evidence(self):
        maa = FakeMaa(
            battle_frames=[png_bytes("life-3"), png_bytes("life-3"),
                           png_bytes("life-2"), png_bytes("life-2")],
            settlement_frame=png_bytes("result-ended-grey"),
            copilot_polls=40,
        )

        result = self.run_offline(maa, manifest_blocked=True)
        summary = self.summary(result)

        self.assertFalse(result["ok"])
        self.assertEqual(result["battle_result"], "aborted_on_leak")
        self.assertEqual(result["program_status"], "completed")
        self.assertEqual(result["failure"]["reason"], "leak")
        self.assertTrue(result["failure"]["exit_confirmed"])
        self.assertFalse(result["failure"]["abandon_failed"])
        self.assertNotEqual(result["program_status"], "ABANDON_FAILED")

        # 假 MAA 确实被停止过一次（漏怪中断），放弃链在「行动结果」页收尾。
        self.assertGreaterEqual(maa.stops, 1)
        self.assertEqual([name for name, _ in maa.appended].count("Copilot"), 1)
        self.assertIn(["AbandonEntry"],
                      [params.get("task_names") for _, params in maa.appended])

        # 四宫格 PNG + 同名 sidecar 必须落盘，并带真实采样时间。
        evidence = Path(result["evidence_png"])
        self.assertTrue(evidence.is_file(), "漏怪四宫格 PNG 未落盘")
        with Image.open(evidence) as image:
            self.assertEqual(image.format, "PNG")
            self.assertGreater(image.width, 0)
        sidecar_path = evidence.with_suffix(".json")
        self.assertTrue(sidecar_path.is_file(), "漏怪 sidecar 未落盘")
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        self.assertEqual(sidecar["version"], 1)
        self.assertEqual(sidecar["png"], evidence.name)
        self.assertEqual(sidecar["stage_code"], STAGE_CODE)
        self.assertIsNotNone(sidecar["leak"])
        self.assertEqual(len(sidecar["frames"]), 4)
        self.assertTrue(all(frame["sampled_at"] is not None
                            for frame in sidecar["frames"]))
        self.assertIsNone(summary["leak_evidence_error"])

        # 收尾清理失败只留 warning：清单写入失败在 cleanup_warnings 里，但
        # program_status/ok 由原生事件决定，不得因此变成 failed。
        self.assertTrue(summary["cleanup_warnings"], "应注入一条收尾清理 warning")
        self.assertTrue(any("复核清单" in warning
                            for warning in summary["cleanup_warnings"]))
        self.assertEqual(parse_maa_events(Path(result["events_path"]))["program_status"],
                         "completed")
        self.assertEqual(summary["program_status"], "completed")
        self.assertIsNone(result["review_manifest"])

        # 退出确认后归位一次（不再是人工介入），用 patch 的 navigator 记录验证。
        self.assertEqual([call["stage"] for call in self.panel_calls], [STAGE_CODE])

        whats = self.event_whats(result)
        self.assertIn("LeakDetected", whats)
        self.assertIn("LeakEvidence", whats)
        self.assertIn("AbandonActionEnded", whats)
        self.assertNotIn("RunnerError", whats)

        # CopilotAction 的 -1 时间戳由本地战斗秒补齐（事件里必须可读）。
        actions = [json.loads(event["details"]) for event in self.events(result)
                   if '"CopilotAction"' in event["details"]]
        self.assertTrue(actions)
        self.assertGreaterEqual(actions[0]["details"]["elapsed_time"], 0)

    def test_stop_induced_taskchain_error_does_not_fail_program(self):
        """主动停止后的 10000 TaskChainError 只是预期收尾，不是运行失败。"""
        maa = FakeMaa(
            battle_frames=[png_bytes("life-3"), png_bytes("life-3"),
                           png_bytes("life-2"), png_bytes("life-2")],
            settlement_frame=png_bytes("result-ended-grey"),
            copilot_polls=40,
        )

        result = self.run_offline(maa)

        self.assertGreaterEqual(maa.stops, 1, "漏怪必须真的中断 Copilot")
        self.assertEqual(result["battle_result"], "aborted_on_leak")
        self.assertEqual(result["program_status"], "completed")
        self.assertEqual(
            parse_maa_events(Path(result["events_path"]))["program_status"],
            "completed")

        # 被主动停止的 Copilot 链：10000 已降级为 -3 诊断事件，且仍带原 taskid。
        events = self.events(result)
        copilot_errors = [event for event in events
                          if event["message"] == 10000
                          and json.loads(event["details"]).get("taskid")
                          == maa.copilot_task_id]
        self.assertEqual(copilot_errors, [], "主动停止的链不得留下失败事件")
        downgraded = [event for event in events
                      if event["message"] == -3
                      and json.loads(event["details"]).get("taskid")
                      == maa.copilot_task_id]
        self.assertTrue(downgraded, "主动停止的 TaskChainError 应降级为 -3")

    def test_sidecar_write_failure_is_only_a_runner_warning(self):
        """sidecar 写失败：PNG 仍在，warning 不得把已完成状态改成失败。"""
        maa = FakeMaa(
            battle_frames=[png_bytes("life-3"), png_bytes("life-3"),
                           png_bytes("life-2"), png_bytes("life-2")],
            settlement_frame=png_bytes("result-ended-grey"),
            copilot_polls=40,
        )
        original_write_text = Path.write_text

        def failing_write_text(path, data, *args, **kwargs):
            if path.name.startswith("leak-evidence-") and path.suffix == ".json":
                raise OSError("磁盘已满（离线注入）")
            return original_write_text(path, data, *args, **kwargs)

        with mock.patch.object(Path, "write_text", failing_write_text):
            result = self.run_offline(maa)
        summary = self.summary(result)

        self.assertEqual(result["program_status"], "completed")
        self.assertEqual(result["battle_result"], "aborted_on_leak")
        self.assertTrue(Path(result["evidence_png"]).is_file())
        self.assertFalse(Path(result["evidence_png"]).with_suffix(".json").exists())
        self.assertEqual(len(summary["cleanup_warnings"]), 1)
        self.assertIn("sidecar", summary["cleanup_warnings"][0])
        whats = self.event_whats(result)
        self.assertIn("LeakEvidence", whats)
        self.assertIn("RunnerWarning", whats)
        self.assertNotIn("RunnerError", whats)


class NaturalCopilotFailureIntegrationTest(RunnerIntegrationBase):
    """对照：未经主动停止的 Copilot 10000 仍必须落 failed。"""

    def test_natural_copilot_error_stays_failed(self):
        maa = NaturalCopilotFailureFakeMaa(
            battle_frames=[png_bytes("battle-snow-1")],
            settlement_frame=png_bytes("result-ended-grey"),
            copilot_polls=3,
        )

        result = self.run_offline(maa)

        self.assertEqual(maa.stops, 0, "自然失败不得被当成主动停止")
        self.assertEqual(result["program_status"], "failed")
        self.assertFalse(result["ok"])
        self.assertEqual(
            parse_maa_events(Path(result["events_path"]))["program_status"],
            "failed")
        self.assertIn(10000, [event["message"] for event in self.events(result)])


class ThreeStarWinIntegrationTest(RunnerIntegrationBase):
    """B. MarkBattleWon + 三星奖章：唯一可以产出的 won。"""

    def _run(self, settlement_frame: str, **kwargs) -> tuple[dict, dict, FakeMaa]:
        maa = FakeMaa(battle_frames=[png_bytes("battle-snow-1")],
                      settlement_frame=png_bytes(settlement_frame),
                      copilot_polls=3, **kwargs)
        result = self.run_offline(maa)
        return result, self.summary(result), maa

    def test_three_star_settlement_is_verified_win(self):
        result, summary, maa = self._run("result-cleared")

        self.assertTrue(result["ok"])
        self.assertEqual(result["battle_result"], "won")
        self.assertEqual(result["program_status"], "completed")
        self.assertIsNone(result["failure"])
        self.assertIsNone(result["evidence_png"])
        self.assertTrue(result["review_manifest"])
        self.assertEqual(summary["win_evidence"], "verified")
        self.assertEqual(summary["native_battle"], "won")
        self.assertTrue(summary["exit_confirmed"])
        self.assertEqual(summary["cleanup_warnings"], [])

        manifest = json.loads(
            Path(result["review_manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(manifest["battle_result"], "won")
        self.assertTrue(manifest["ok"])

        # DEFAULT_MAA/DEFAULT_ADB/DEFAULT_SERIAL 被 patch 后必须原样传给原生绑定；
        # MAA user 目录也落在临时目录，不写真实 data/maa_userdata。
        self.assertEqual(maa.loaded[0]["path"], str(self.maa_dir))
        self.assertEqual(maa.loaded[0]["user_dir"], str(self.maa_user))
        self.assertEqual(maa.connections[0][0], str(self.base / "adb.exe"))
        self.assertEqual(maa.connections[0][1], "offline-serial")
        self.assertEqual(maa.connections[0][2], "MuMuEmulator12")
        self.assertEqual(maa.instance_options, [("touch_type", "maatouch")])

        # 链序固定：前置检查 → Copilot → 失败探测（未命中）→ 胜利探测。
        self.assertEqual([name for name, _ in maa.appended],
                         ["Custom", "Copilot", "Custom", "Custom"])
        self.assertEqual(
            [params.get("task_names") for name, params in maa.appended
             if name == "Custom"],
            [["BattleReadyCheck"], ["ResultFailedCheck"], ["ResultWinCheck"]])
        self.assertEqual(maa.starts, 4)
        self.assertEqual(maa.stops, 0, "已复核的三星胜利不应触发停止/放弃链")
        self.assertEqual([call["stage"] for call in self.panel_calls], [STAGE_CODE])

        whats = self.event_whats(result)
        self.assertIn("BattleWonEvidence", whats)
        self.assertNotIn("BattleWonUnproven", whats)
        self.assertNotIn("RunnerError", whats)
        verdicts = [json.loads(event["details"]) for event in self.events(result)
                    if '"BattleWonEvidence"' in event["details"]]
        self.assertEqual(verdicts[0]["verdict"], "verified")

    def test_win_with_recoverable_cleanup_warning_stays_ok(self):
        """B2. 三星胜利 + 可恢复收尾警告：警告只丢证据，不改判胜利。

        装配与 B 相同（MarkBattleWon + 三蓝章 ``result-cleared``），只把 finally
        的收尾 ``running()`` 换成抛错的假 MAA：``cleanup_warnings`` 必须非空，
        但 ``ok``/胜负/状态/证据一个都不许被改写，事件层只能出现
        ``RunnerWarning``。
        """
        maa = CleanupWarningFakeMaa(
            battle_frames=[png_bytes("battle-snow-1")],
            settlement_frame=png_bytes("result-cleared"),
            copilot_polls=3)

        result = self.run_offline(maa)
        summary = self.summary(result)

        self.assertTrue(result["ok"])
        self.assertEqual(result["battle_result"], "won")
        self.assertEqual(result["program_status"], "completed")
        self.assertEqual(summary["win_evidence"], "verified")
        self.assertIsNone(result["failure"])
        self.assertIsNone(summary["runtime_error"])
        self.assertEqual(summary["native_battle"], "won")
        self.assertTrue(summary["exit_confirmed"])

        # 收尾失败只进 warning 列表：不得把 completed 改判成 failed。
        self.assertEqual(len(summary["cleanup_warnings"]), 1)
        self.assertIn("MAA 停止异常", summary["cleanup_warnings"][0])
        self.assertIn("offline-injected", summary["cleanup_warnings"][0])
        self.assertEqual(
            parse_maa_events(Path(result["events_path"]))["program_status"],
            "completed")

        # 事件层必须是 RunnerWarning（消息号 2），绝不能被写成 RunnerError。
        whats = self.event_whats(result)
        self.assertIn("RunnerWarning", whats)
        self.assertNotIn("RunnerError", whats)
        warning_events = [event for event in self.events(result)
                          if '"RunnerWarning"' in event["details"]]
        self.assertEqual(len(warning_events), 1)
        self.assertEqual(warning_events[0]["message"], 2)
        self.assertIn("offline-injected",
                      json.loads(warning_events[0]["details"])["why"])

        # 警告不吞证据：复核清单仍落盘，且仍记录同一场胜利。
        self.assertTrue(Path(result["review_manifest"]).is_file())
        manifest = json.loads(
            Path(result["review_manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(manifest["battle_result"], "won")
        self.assertTrue(manifest["ok"])

    def test_reposition_failure_after_exit_is_reposition_failed(self):
        """D. exit_confirmed 后归位失败：落 REPOSITION_FAILED，战报仍完整。

        复用三星胜利装配，只把归位换成抛 ``PanelRecoveryError`` 的假导航。
        归位只能在确认离开战斗后调用一次（战斗前面板已确认，不走前置归位），
        因此这条异常必须来自战后归位，而不是被前置归位的运行异常抢戏。
        """
        def failing_panel(stage_code, **kwargs):
            self.panel_calls.append({"stage": stage_code, **kwargs})
            raise runner.PanelRecoveryError(
                f"自动归位到关卡 {stage_code} 详情面板失败：offline-injected")

        maa = FakeMaa(battle_frames=[png_bytes("battle-snow-1")],
                      settlement_frame=png_bytes("result-cleared"),
                      copilot_polls=3)

        result = self.run_offline(maa, panel_side_effect=failing_panel)
        summary = self.summary(result)

        # 战果不被归位失败改写；状态与失败原因必须是归位这一条事实。
        self.assertFalse(result["ok"])
        self.assertEqual(result["program_status"], "REPOSITION_FAILED")
        self.assertEqual(result["battle_result"], "won")
        failure = result["failure"]
        self.assertIsNotNone(failure)
        self.assertEqual(failure["reason"], "reposition_failed")
        self.assertTrue(failure["exit_confirmed"])
        self.assertTrue(failure["reposition_error"])
        self.assertIn("PanelRecoveryError", failure["reposition_error"])
        self.assertIn("offline-injected", failure["reposition_error"])

        # 归位失败绝不能吞掉结构化战报：maa-run.json 仍在，且记录同一事实。
        report_path = Path(result["report_path"])
        self.assertEqual(report_path.name, "maa-run.json")
        self.assertTrue(report_path.is_file(), "归位失败后 maa-run.json 未落盘")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["program_status"], "REPOSITION_FAILED")
        self.assertFalse(report["ok"])
        self.assertEqual(report["battle_result"], "won")
        self.assertTrue(report["exit_confirmed"])
        self.assertEqual(report["reposition_error"],
                         failure["reposition_error"])

        # 更强的失败事实不成立，归位失败才降级状态：不得借 runtime/清理警告
        # 之类的记账掩盖或伪造真正的失败，也不得把已复核的胜利抹掉。
        self.assertIsNone(summary["runtime_error"])
        self.assertEqual(summary["cleanup_warnings"], [])
        self.assertEqual(summary["native_battle"], "won")
        self.assertEqual(summary["win_evidence"], "verified")
        self.assertEqual([call["stage"] for call in self.panel_calls],
                         [STAGE_CODE], "归位只应在确认退出后尝试一次")

        whats = self.event_whats(result)
        self.assertIn("BattleWonEvidence", whats)
        self.assertIn("RepositionError", whats)
        self.assertNotIn("RunnerError", whats)
        self.assertEqual(
            parse_maa_events(Path(result["events_path"]))["program_status"],
            "completed", "原生链本身仍应记为 completed")

    def test_grey_medal_settlement_cannot_be_won(self):
        """灰章结算页（rejected）：战斗结束成立，但绝不判胜利。"""
        result, summary, _maa = self._run("result-ended-grey")

        self.assertFalse(result["ok"])
        self.assertEqual(result["battle_result"], "unknown")
        self.assertNotEqual(result["battle_result"], "won")
        self.assertEqual(summary["win_evidence"], "rejected")
        self.assertTrue(result["failure"]["reason"].startswith("battle_unknown"))
        whats = self.event_whats(result)
        self.assertIn("BattleWonUnproven", whats)
        self.assertNotIn("BattleWonUnverified", whats)

    def test_unverified_win_mark_cannot_be_won(self):
        """抓帧失败（unverified）：保留原生标记，但最终胜负只能是 unknown。"""
        result, summary, _maa = self._run(
            "result-cleared", images_fail_after_battle=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["battle_result"], "unknown")
        self.assertNotEqual(result["battle_result"], "won")
        self.assertEqual(summary["win_evidence"], "unverified")
        self.assertIn("BattleWonUnverified", self.event_whats(result))


class AbandonFailureProbeIntegrationTest(RunnerIntegrationBase):
    """E. 放弃链在结算页上失败：等渲染后复查结果页，不能直接 ABANDON_FAILED。

    实机验收 A2：放弃链在「行动结束」渲染前就启动，AbandonEntry 盲点
    BattlePause 失败后直接以 ``MarkAbandonFailed`` 收尾；失败优先的复查又只
    发了 ResultFailedCheck（未命中），ResultWinCheck 从未 append。修复后先等
    结算页渲染，再以 ``ResultWinCheck`` 优先复查，命中只作退出。
    """

    def test_abandon_failure_win_check_first_confirms_ended(self):
        """MarkAbandonFailed → ResultFailedCheck 未命中 → ResultWinCheck 命中。

        失败页脚本仍是「未识别到关键字」：修复前它先跑、未命中，第二条链没
        机会 append；修复后 ResultWinCheck 先跑并命中通用「行动结果」页，
        ResultFailedCheck 根本不需要再跑。
        """
        scripts = dict(TASK_SCRIPTS)
        # 放弃链自身失败：只有 MarkAbandonFailed，没有任何结算证据。
        scripts["AbandonEntry"] = {"marks": ["MarkAbandonFailed"]}
        maa = FakeMaa(
            battle_frames=[png_bytes("battle-snow-1")],
            settlement_frame=png_bytes("result-ended-grey"),
            scripts=scripts, copilot_polls=40)

        result = self.run_offline(maa, timeout=1)
        summary = self.summary(result)

        # 探测顺序固定：放弃链 → （等渲染）→ ResultWinCheck 先跑并命中。
        custom_probes = [params.get("task_names")
                         for name, params in maa.appended if name == "Custom"]
        self.assertEqual(custom_probes,
                         [["BattleReadyCheck"], ["AbandonEntry"],
                          ["ResultWinCheck"]])

        details = [json.loads(event["details"]) for event in self.events(result)]
        whats = self.event_whats(result)
        self.assertIn("AbandonResultProbeWait", whats)
        self.assertLess(whats.index("AbandonResultProbeWait"),
                        whats.index("AbandonProbeAppend"),
                        "必须先等结算页渲染再 append 探测链")
        hit = [d for d in details if d.get("what") == "AbandonProbeHit"]
        self.assertEqual([(d["task"], d["mark"]) for d in hit],
                         [("ResultWinCheck", "MarkBattleWon")])

        self.assertIsNotNone(result["failure"])
        self.assertTrue(result["failure"]["exit_confirmed"],
                        "结算页探测命中后必须确认退出")
        self.assertFalse(result["failure"]["abandon_failed"])
        self.assertNotEqual(result["program_status"], "ABANDON_FAILED")
        self.assertNotEqual(result["program_status"], "failed")

        # 「won」只用于退出：灰色「行动结果」页不能被当成胜利。
        self.assertEqual(result["battle_result"], "unknown")
        self.assertEqual(summary["native_battle"], "unknown")
        self.assertEqual(summary["win_evidence"], "rejected")
        self.assertNotEqual(result["battle_result"], "won")

        self.assertIn("AbandonActionEnded", whats)
        self.assertIn("BattleWonUnproven", whats)
        self.assertNotIn("RunnerError", whats)

    def test_abandon_failure_falls_back_to_failed_check(self):
        """ResultWinCheck 未命中后，才跑 ResultFailedCheck 并按其 result_page 退出。"""
        scripts = dict(TASK_SCRIPTS)
        scripts["AbandonEntry"] = {"marks": ["MarkAbandonFailed"]}
        scripts["ResultWinCheck"] = {"error": "未识别到关键字（离线注入）"}
        scripts["ResultFailedCheck"] = {"marks": ["MarkBattleFailed"]}
        maa = FakeMaa(
            battle_frames=[png_bytes("battle-snow-1")],
            settlement_frame=png_bytes("result-failed"),
            scripts=scripts, copilot_polls=40)

        result = self.run_offline(maa, timeout=1)

        # 先试结算页，未命中后才落到失败页；两条链都真的 append 了。
        custom_probes = [params.get("task_names")
                         for name, params in maa.appended if name == "Custom"]
        self.assertEqual(custom_probes,
                         [["BattleReadyCheck"], ["AbandonEntry"],
                          ["ResultWinCheck"], ["ResultFailedCheck"]])

        details = [json.loads(event["details"]) for event in self.events(result)]
        appends = [d["task"] for d in details
                   if d.get("what") == "AbandonProbeAppend"]
        self.assertEqual(appends, ["ResultWinCheck", "ResultFailedCheck"])
        hit = [d for d in details if d.get("what") == "AbandonProbeHit"]
        self.assertEqual([(d["task"], d["mark"]) for d in hit],
                         [("ResultFailedCheck", "MarkBattleFailed")])

        self.assertIsNotNone(result["failure"])
        self.assertTrue(result["failure"]["exit_confirmed"])
        self.assertFalse(result["failure"]["abandon_failed"])
        self.assertNotEqual(result["program_status"], "ABANDON_FAILED")
        self.assertEqual(result["battle_result"], "failed")

        whats = self.event_whats(result)
        self.assertIn("AbandonResultPage", whats)
        self.assertNotIn("BattleWonUnproven", whats)
        self.assertNotIn("RunnerError", whats)


if __name__ == "__main__":
    unittest.main()

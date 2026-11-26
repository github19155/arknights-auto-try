#!/usr/bin/env python3
"""原生 MAA Copilot 执行器；默认演习，失败时保存最近四帧证据。

前置条件：游戏已连接并停在目标关卡详情面板。战斗结束后尝试归位；
退出或归位未确认时需人工恢复，不把停止自动化当成已退出战斗。
MAA chain completed 只表示程序完成，不等于战斗胜利。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import threading
import io
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from PIL import Image

from src.engine.recorder import (
    BATTLE_CONTROL_ROI, BATTLE_PAUSE_BUTTON, BATTLE_PAUSE_MENU,
    battle_scene_state,
    build_failure_report, compose_evidence_grid, hp_digit_converged,
    hp_digit_ready, hp_digit_shape, hp_digit_shape_changed, hp_signature,
    is_battle_hud, load_projection_meta, parse_maa_events, result_three_star,
    select_evidence_frames,
)

DEFAULT_MAA = os.environ.get("MAA_DIR", "maa")
DEFAULT_ADB = os.environ.get("MAA_ADB", "adb")
DEFAULT_SERIAL = os.environ.get("MAA_SERIAL", "127.0.0.1:5555")
ROOT = Path(__file__).resolve().parents[2]
RESULT_PROBE = ROOT / "tools" / "maa-resources" / "result-probe"
PREFLIGHT = ROOT / "tools" / "maa-resources" / "preflight"
ABANDON_BATTLE = ROOT / "tools" / "maa-resources" / "abandon-battle"
ABANDON_HUD_TIMEOUT = 20
# 放弃链会早于「行动结束」渲染启动：复查前必须先等结算页画出来（真机实测
# 放弃链启动→结算页可识别约 2s）。
ABANDON_RESULT_RENDER_WAIT = 2.5
# 放弃失败后的复查顺序：先结算页（行动结束/胜利都会命中 MarkBattleWon），
# 没命中再问任务失败页。两条只读链的超时与间隔分开，避免复用结果探测的
# 失败优先顺序。
ABANDON_WIN_PROBE_TIMEOUT = 25
ABANDON_FAILED_PROBE_TIMEOUT = 20
ABANDON_PROBE_GAP = 0.5
MODE_OVERLAY = {
    "exercise": "exercise-only",
    "official": "official-only",
    "formation": "formation-only",
}

VIDEO_NAME = "battle.mp4"
REVIEW_MANIFEST_NAME = "review-manifest.json"

# 收尾清理失败（录屏拉取、清单/sidecar 写入、补截帧）只丢证据，不是运行失败。
# 这类事实写 RunnerWarning + 非失败事件号：parse_maa_events 只把 RunnerError /
# CaptureError / 失败消息号映射成 failed，已复核的胜利不得因收尾失败变成失败。
CLEANUP_WARNING_MESSAGE = 2
CLEANUP_WARNING_WHAT = "RunnerWarning"


def cleanup_warning_event(why: str) -> tuple[int, dict]:
    """收尾清理失败的写入载荷（纯函数）：只做诊断，不参与 program_status。"""
    return CLEANUP_WARNING_MESSAGE, {"what": CLEANUP_WARNING_WHAT, "why": why}


_TIME_BASIS = {
    "clock": "time.monotonic（同一进程内的单调时钟，不写绝对墙钟）",
    "video_zero": ("adb screenrecord 进程启动成功时的 monotonic 时刻，"
                   "记为 battle.mp4 的 0 秒"),
    "battle_zero": ("Copilot append_task + start 成功后、帧采样线程启动前的 "
                    "monotonic 时刻"),
    "battle_start": ("视频秒 = battle_zero - video_zero；包含录屏启动后等待的 "
                     "1.5s 准备时间，不能把这段等待当作战斗时间"),
    "leak_time": ("视频秒 = 漏怪确认时刻 - video_zero；无漏怪时省略该键"
                  "（battle_review 把 null 视为非法锚点），以 leak_detected=false 表达"),
    "leak_battle_s": "战斗秒 = 漏怪确认时刻 - battle_zero",
    "deviation": ("screenrecord 编码器首帧延迟与 adb 拉取时钟抖动未补偿，通常 "
                  "<0.3s；LeakDetected 取连续两帧确认时刻，比真实首次扣血晚约 1 个 "
                  "150ms 采样节拍；MAA 原生 elapsed_time 以游戏战斗开始为原点，"
                  "本地战斗秒以 Copilot start 为原点，二者可能相差入场加载时间，"
                  "因此原生有效值优先保留"),
}


class ReviewTimeline:
    """战斗/录像时间锚点；clock 可注入，默认 time.monotonic。

    video_t0 记录 screenrecord 进程启动时刻，battle_t0 记录 Copilot
    append/start 成功并即将进入采样的时刻；二者都来自同一单调时钟，
    因此 battle_start（视频秒）与 leak_time（视频秒）可直接换算。
    """

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self.video_t0: float | None = None
        self.battle_t0: float | None = None
        self.leak_t0: float | None = None

    def mark_video_start(self) -> float:
        self.video_t0 = self._clock()
        return self.video_t0

    def mark_battle_start(self) -> float:
        self.battle_t0 = self._clock()
        return self.battle_t0

    def mark_leak(self) -> float:
        self.leak_t0 = self._clock()
        return self.leak_t0

    def battle_s(self) -> float | None:
        if self.battle_t0 is None:
            return None
        return self._clock() - self.battle_t0

    def sample_stamp(self, now: float | None = None) -> dict:
        """一帧的单调时钟与相对时间锚点；同一 ``now`` 保证三个键一致。

        采样线程每抓一帧写进 ``frame.info``，证据四宫格和传感事实都从这里
        取时间，不再只靠帧序号（帧序号会因丢帧/降采样与真实时间脱节）。
        """
        if now is None:
            now = self._clock()
        battle_s = None if self.battle_t0 is None else max(0.0, now - self.battle_t0)
        video_s = None if self.video_t0 is None else max(0.0, now - self.video_t0)
        return {
            "sampled_at": round(now, 6),
            "battle_s": None if battle_s is None else round(battle_s, 3),
            "video_s": None if video_s is None else round(video_s, 3),
        }

    def battle_start_video_s(self) -> float | None:
        if self.video_t0 is None or self.battle_t0 is None:
            return None
        return max(0.0, self.battle_t0 - self.video_t0)

    def leak_battle_s(self) -> float | None:
        if self.leak_t0 is None or self.battle_t0 is None:
            return None
        return max(0.0, self.leak_t0 - self.battle_t0)

    def leak_video_s(self) -> float | None:
        if self.video_t0 is None or self.leak_t0 is None:
            return None
        return max(0.0, self.leak_t0 - self.video_t0)


def _fill_elapsed_time(payload: dict, elapsed_s: float | None) -> bool:
    """就地修正 runner 事件副本中的 CopilotAction 时间戳；返回是否写入。

    MAA 的 ``details.elapsed_time`` 为战斗秒；-1/缺失/非法时用本地单调时钟
    相对战斗开始的秒数补齐。原始值有效（有限非负数）时保留，避免覆盖 MAA
    更精确的原生计时。
    """
    action_details = payload.get("details")
    if not isinstance(action_details, dict):
        return False
    current = action_details.get("elapsed_time")
    if (isinstance(current, (int, float)) and not isinstance(current, bool)
            and math.isfinite(float(current)) and current >= 0):
        return False
    if elapsed_s is None or not math.isfinite(float(elapsed_s)) or elapsed_s < 0:
        return False
    action_details["elapsed_time"] = round(float(elapsed_s), 3)
    return True


def _build_review_manifest(timeline: ReviewTimeline, *, video_started: bool,
                           video_available: bool,
                           warnings: list[str] | None = None) -> dict:
    """生成 battle_review 可读的复核清单；录像不可用时不伪造视频锚点。"""
    notes = list(warnings or [])
    video_ok = bool(video_started and video_available)
    if not video_started:
        notes.append("未开启录屏（record_video=False）：没有 battle.mp4，"
                     "视频秒锚点不可用")
    elif not video_ok:
        notes.append("录屏已启动但未拉取到可用的 battle.mp4：视频秒锚点不可用")
    if timeline.battle_t0 is None:
        notes.append("Copilot 未成功进入采样：battle_start 未知")
    if timeline.leak_t0 is None:
        notes.append("未检测到漏怪：leak_time 为空")

    battle_start = timeline.battle_start_video_s() if video_ok else None
    leak_time = timeline.leak_video_s() if video_ok else None
    leak_battle_s = timeline.leak_battle_s()
    manifest = {
        "version": 1,
        "video": {
            "path": VIDEO_NAME if video_ok else None,
            "recorded": bool(video_started),
            "available": video_ok,
        },
        "leak_detected": timeline.leak_t0 is not None,
        "time_basis": dict(_TIME_BASIS),
        "warnings": notes,
    }
    # battle_review 把存在的 null 锚点视为非法，因此缺失的锚点直接省略。
    if battle_start is not None:
        manifest["battle_start"] = round(battle_start, 3)
    if leak_time is not None:
        manifest["leak_time"] = round(leak_time, 3)
    if leak_battle_s is not None:
        manifest["leak_battle_s"] = round(leak_battle_s, 3)
    return manifest


def _write_review_manifest(path: Path, manifest: dict) -> None:
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def battle_evidence(scene: str, *, actions: int, leaked: bool) -> bool:
    """战斗是否真的开始了：画面态、CopilotAction 或漏怪任一成立。

    ``scene`` 来自 ``battle_scene_state``；``not_battle``/``unknown`` 本身不能
    证明战斗开始，但只要有动作（Copilot 已执行）或漏怪，战斗就已被证实在
    进行过，超时/异常时必须走放弃而不是归位。
    """
    return leaked or actions > 0 or scene in {"live", "paused"}


def battle_exit_verdict(scene: str, marks) -> str:
    """退出裁决：只有「非战斗画面 + 原生退出标记」才算 exited。

    live/paused 永远是 in_battle（暂停菜单、选中干员遮罩都不能当退出）；
    没有原生 ``MarkBattleAbandoned`` 证据或画面未知时只能返回 unknown，
    调用方不得据此导航或报成功。
    """
    if scene in {"live", "paused"}:
        return "in_battle"
    if scene == "not_battle" and "MarkBattleAbandoned" in marks:
        return "exited"
    return "unknown"


def abandon_result_verdict(marks) -> str | None:
    """放弃链自带 OCR 闸门的裁决：结算页证据只说明「战斗已经结束」。

    ``result_page``：失败守卫命中（``MarkAbandonResultPage``，原生「任务失败」
    OCR）；``ended``：通用结算守卫命中（``MarkAbandonActionEnded``，原生
    「行动结束/MISSION RESULTS」OCR）。两者都只证明战斗结束，既不是胜利也
    不是失败：调用方还须过 ``ended_exit_allowed`` 的画面复核，通过后才按
    「已退出」处理，battle 结果保持 unknown；失败守卫优先。
    都未命中返回 None（调用方继续走原生放弃链）。
    """
    if "MarkAbandonResultPage" in marks:
        return "result_page"
    if "MarkAbandonActionEnded" in marks:
        return "ended"
    return None


def ended_exit_allowed(scene: str) -> bool:
    """结算证据之后才允许确认退出：画面必须真的离开战斗。

    OCR 闸门只证明「战斗已经结束」，不证明画面已经离开战斗——结算遮罩、
    暂停遮罩、抓帧失败都可能留下来。``live``/``paused``/``unknown`` 一律
    不成立：调用方不得置 ``exit_confirmed``，只能保留人工处理。
    """
    return scene == "not_battle"


class LeakSampler:
    """扣血采样状态机：锁定收敛基线 → 连续两帧确认。

    基线只在「HUD 生命点出现 + 数字字形已画完 + 相邻两帧命中数停止爬升且
    亮度不再升高」时锁定。HUD 渐入期间，即使命中数已达到阈值、
    相邻字形差较小，也可能尚未完全显示；过早锁定会把后续正常变亮
    误判为扣血。

    扣血只看数字字形结构差：生命点像素数在整屏压暗时会塌缩，压暗不是扣血；
    命中数显著增加（画面变亮）同样不是扣血。

    每帧的采样时刻由调用方写入 ``frame.info``（单调时钟 + 战斗/视频相对秒），
    传感事实原样带出基线/首变/确认三个锚点，证据清单据此可追溯到真实时间，
    而不是只靠帧序号。
    """

    #: 传感事实里必须带出的时间锚点（frame.info 键）。
    TIME_KEYS = ("sampled_at", "battle_s", "video_s")

    def __init__(self) -> None:
        self.baseline_sig: tuple | None = None
        self.baseline_shape: tuple[bytes, int] | None = None
        self.baseline_frame_index: int | None = None
        self.baseline_frame = None
        self.first_change_frame = None
        self.first_changed_frame_index: int | None = None
        self.confirmed = False
        self._pending_shape: tuple[bytes, int] | None = None
        self._pending_frame = None
        self._changed_samples = 0

    @classmethod
    def _stamp(cls, frame) -> dict:
        """取一帧的真实采样时间；缺 info 时返回 None，不用帧序号冒充时间。"""
        info = getattr(frame, "info", None) or {}
        return {key: info.get(key) for key in cls.TIME_KEYS}

    def observe(self, frame, *, frame_index: int) -> dict | None:
        """喂一帧采样；确认扣血时返回传感事实，否则 None。"""
        if self.confirmed:
            return None
        shape = hp_digit_shape(frame)
        if self.baseline_shape is None:
            # 命中数不够（渐入/压暗）或相邻帧尚未收敛：不能锁基线。
            if not hp_digit_ready(shape) or not is_battle_hud(frame):
                self._pending_shape = None
                self._pending_frame = None
                return None
            if self._pending_shape is None:
                self._pending_shape = shape
                self._pending_frame = frame
                return None
            if not hp_digit_converged(self._pending_shape, shape,
                                      prev_frame=self._pending_frame,
                                      cur_frame=frame):
                self._pending_shape = shape
                self._pending_frame = frame
                return None
            self.baseline_shape = self._pending_shape
            self.baseline_sig = hp_signature(frame)
            self.baseline_frame_index = frame_index
            self.baseline_frame = self._pending_frame
            self._pending_shape = None
            self._pending_frame = None
            return None
        if not hp_digit_shape_changed(self.baseline_shape, shape):
            self._changed_samples = 0
            return None
        self._changed_samples += 1
        if self._changed_samples < 2:
            self.first_change_frame = frame
            self.first_changed_frame_index = frame_index
            return None
        self.confirmed = True
        baseline = self._stamp(self.baseline_frame)
        first_change = self._stamp(self.first_change_frame)
        confirmed = self._stamp(frame)
        return {
            "confirmed_at_frame_index": frame_index,
            "first_changed_frame_index": self.first_changed_frame_index,
            "baseline_frame_index": self.baseline_frame_index,
            "baseline_sampled_at": baseline["sampled_at"],
            "baseline_battle_s": baseline["battle_s"],
            "baseline_video_s": baseline["video_s"],
            "first_changed_sampled_at": first_change["sampled_at"],
            "first_changed_battle_s": first_change["battle_s"],
            "first_changed_video_s": first_change["video_s"],
            "confirmed_sampled_at": confirmed["sampled_at"],
            "confirmed_battle_s": confirmed["battle_s"],
            "confirmed_video_s": confirmed["video_s"],
            "digit_baseline_hits": self.baseline_shape[1],
            "digit_current_hits": shape[1],
            "digit_shape_changed": True,
            "hp_before": self.baseline_sig,
            "hp_after": hp_signature(frame),
        }


def evidence_with_pin(chosen: list, pin) -> list:
    """证据四格：把首扣血现场换进最旧的那一格里。

    ``chosen`` 已是时间顺序，末位是冻结前的最近有效帧（必须保留）。首扣血帧
    通常比确认帧早一个采样节拍，已在四格里时原样返回，避免重复画面。
    """
    if pin is None or any(pin is frame for frame in chosen):
        return chosen
    return [pin] + chosen[1:]


def evidence_frame_meta(frames: list, *, pin=None) -> list[dict]:
    """证据四格的可追溯清单：每格写真实采样时间，缺失就留 null。

    时间来自采样线程写入 ``frame.info`` 的单调时钟与战斗/视频相对秒；帧序号
    只作辅助定位，排序/复核一律以 ``sampled_at`` 为准。``pin`` 是首扣血现场
    帧，标记出来便于复核哪一格对应首次字形变化。
    """
    meta = []
    for slot, frame in enumerate(frames, 1):
        info = getattr(frame, "info", None) or {}
        meta.append({
            "slot": slot,
            "source": "first_change_pin" if frame is pin else "ring",
            "frame_index": info.get("frame_index"),
            "sampled_at": info.get("sampled_at"),
            "battle_s": info.get("battle_s"),
            "video_s": info.get("video_s"),
        })
    return meta


def leak_evidence_payload(*, png: str | Path, stage_code: str,
                          leak: dict | None, frames: list[dict] | None) -> dict:
    """四宫格 sidecar 的载荷：纯数据组装，不读帧、不碰设备与网络。

    ``png`` 只保留文件名：sidecar 与 PNG 同目录，写绝对路径在搬移录屏目录
    或换机后就会失效。``time_basis`` 随证据一起冻结，复核时才能确认这一份
    采样时间用的是哪个时钟锚点。
    """
    return {
        "version": 1,
        "png": Path(png).name,
        "stage_code": stage_code,
        "time_basis": dict(_TIME_BASIS),
        "leak": leak,
        "frames": frames,
    }


# 结算页证据集合：failed（任务失败）、won（有胜利标记且画面三星）、
# ended（战斗确实结束，但胜利未获视觉证据）。三者都只用于确认退出，
# 胜负另由 battle_result_verdict 判定。
RESULT_PAGE_VERDICTS = ("failed", "won", "ended")


def exit_decision(scene: str, *, panel_confirmed: bool,
                  result_page: str | None) -> str:
    """Copilot 完成后的退出裁决（纯函数，便于离线复核）。

    返回：
      - ``panel``：原生 BattleReadyCheck 已确认停在关卡详情面板，退出成立；
      - ``result_page``：画面非战斗且原生 OCR/模板确认了结算页，战斗已结束，
        直接归位，**绝不点暂停**。``ended`` 同样算退出证据，但只说明战斗
        结束，不代表胜利；
      - ``abandon``：画面仍是活战斗或暂停态，走原生放弃链（有暂停证据支撑）；
      - ``unknown``：其余情况——不宣称退出，也不在非战斗画面上盲点暂停。

    实测任务失败页正是最后一类：右上控制块消失但没有任何结算证据时，旧实现
    把它当活战斗，AbandonEntry 盲点 BattlePause 三次后 abandon_failed。
    """
    if panel_confirmed:
        return "panel"
    if scene == "not_battle" and result_page in RESULT_PAGE_VERDICTS:
        return "result_page"
    if scene in {"live", "paused"}:
        return "abandon"
    return "unknown"


def battle_result_verdict(marks, result_page: str | None) -> str:
    """把结算证据映射成 battle 结果（纯函数，便于离线复核）。

    ``MarkBattleWon`` 是否保留由 ``_won_verdict`` 决定：只有当前帧三星奖章
    复核通过才产出 ``won``；复核失败（抓帧失败/画面未知）时标记可保留作
    原始证据，但最终由 ``verified_native_battle`` 降为 ``unknown``，绝不把
    通用结算页当胜利。``ended`` 只证明战斗结束，**绝不**映射成 ``won``，
    也不映射成 ``failed``。

    失败标记优先于胜利标记：放弃链自带的失败 OCR 是更强证据。
    """
    if "MarkBattleFailed" in marks:
        return "failed"
    if "MarkBattleWon" in marks:
        return "won"
    if result_page == "failed":
        return "failed"
    if result_page == "won":
        return "won"
    return "unknown"


def won_evidence_verdict(scene: str, *, frame_present: bool,
                         three_star: bool) -> str:
    """``MarkBattleWon`` 的画面复核结论（纯函数，便于离线复核）。

    ``MarkBattleWon`` 只说明结算页出现，胜利必须由**当前帧**的三枚蓝色奖章
    证明。只有 ``not_battle`` + 有效帧才能下结论：

      - ``verified``：三枚奖章都在；
      - ``rejected``：画面已离开战斗，但奖章不成立（1/2 星、灰色奖章、通用
        「行动结束/行动结果」页）——战斗结束，不判胜利；
      - ``unverified``：抓帧失败、``unknown``、``live``/``paused``——画面不给
        结论。此时绝不能销毁原生标记，也绝不能据此判胜，留给重试或人工复核。
    """
    if scene != "not_battle" or not frame_present:
        return "unverified"
    return "verified" if three_star else "rejected"


def verified_native_battle(native_battle: str, win_evidence: str | None) -> str:
    """最终胜负门禁：没有复核过的 ``MarkBattleWon`` 一律不得产出 ``won``。

    原生标记可能在抓帧失败时保留（``won_evidence_verdict`` 的
    ``unverified``）；保留原始证据不等于承认胜利，最终战报仍必须是
    ``unknown``，由人工按录像/证据帧复核。
    """
    if native_battle == "won" and win_evidence != "verified":
        return "unknown"
    return native_battle


ABANDON_BATTLE_RESULTS = {
    "leak": "abandoned_on_leak",
    "timeout": "abandoned_on_timeout",
    "error": "abandoned_on_error",
    "unconfirmed": "abandoned_on_unconfirmed",
}
DEFAULT_FAILURE_REASON = "未收到 MAA TaskChainCompleted/TaskChainError 事件"


def select_battle_result(*, mode: str, formation_no_battle: bool,
                         native_battle: str, abandoned: bool,
                         abandon_reason: str | None, aborted_on_leak: bool,
                         abandon_failed: bool) -> str:
    """选定 battle_result（纯函数，便于离线复核）。

    优先级：编队结果 → 原生胜负 → 放弃/漏怪记账 → ``unknown``。``aborted_on_leak``
    与 ``abandon_failed`` 只是本工具的中断/收尾记账，战斗一旦拿到原生
    ``MarkBattleWon``/``MarkBattleFailed``（或原生结算页证据），就以原生结果
    为准，绝不因漏怪秒退或放弃链失败把已结算的胜负改写掉。``ended`` 只证明
    战斗结束，``battle_result_verdict`` 已把它归为 ``unknown``，这里同样不判
    胜负，``ok`` 必然为假。
    """
    if mode == "formation":
        return "not_started" if formation_no_battle else "formed"
    if native_battle in {"won", "failed"}:
        return native_battle
    if abandoned:
        if abandon_reason not in ABANDON_BATTLE_RESULTS:
            raise ValueError(f"未知放弃原因：{abandon_reason!r}")
        return ABANDON_BATTLE_RESULTS[abandon_reason]
    if aborted_on_leak:
        return "aborted_on_leak"
    if abandon_failed:
        return "abandon_failed"
    return native_battle


def select_run_status(*, formation_no_battle: bool, battle: str,
                      abandon_failed: bool, sanity_blocked: bool,
                      timed_out: bool, runtime_error: str | None,
                      exit_unconfirmed: bool, reposition_error: str | None,
                      program_status: str) -> str:
    """把运行事实映射成 program_status（纯函数）。

    放弃失败、理智不足、超时、运行异常、退出未确认各自保留独立状态；
    只有这些更强的失败都不成立、且本次运行本来会成功时，归位失败才把状态
    降级为 ``REPOSITION_FAILED``（``ok`` 随之必为假）。战斗本身失败
    （``battle == "failed"``）保留 ``program_status``，不拿归位这一附加事实
    覆盖更具体的状态；但结算只证明战斗结束（``ended``/``unknown``）时，
    归位失败同样是本次运行唯一的失败事实，必须落到 ``REPOSITION_FAILED``，
    不能被 ``unknown`` 吞掉。
    """
    if formation_no_battle:
        return "REPOSITION_FAILED" if reposition_error is not None else "completed"
    if abandon_failed:
        return "ABANDON_FAILED"
    if sanity_blocked:
        return "SANITY_EXHAUSTED"
    if timed_out:
        return "timeout"
    if runtime_error is not None and program_status == "unknown":
        return "failed"
    if exit_unconfirmed:
        return "EXIT_UNCONFIRMED"
    if (reposition_error is not None
            and battle in {"won", "not_started", "formed", "unknown", "ended"}
            and program_status in {"completed", "unknown"}):
        return "REPOSITION_FAILED"
    return program_status


def failure_reason(*, abandon_failed: bool, aborted_on_leak: bool,
                   sanity_blocked: bool, reposition_error: str | None,
                   report_reason: str | None) -> str:
    """failure.reason 的优先级（纯函数）。

    放弃失败必须让客户端直接读到 ``abandon_failed``——漏怪事实仍完整保留在
    top-level ``leak`` 对象里，不用 reason 抢戏。归位失败排在状态级失败之后、
    战斗报告之前：只要归位真的失败，reason 就必须看得见它（证据报告仍带
    战斗死因）。
    """
    if abandon_failed:
        return "abandon_failed"
    if aborted_on_leak:
        return "leak"
    if sanity_blocked:
        return "SANITY_EXHAUSTED"
    if reposition_error:
        return "reposition_failed"
    if report_reason:
        return report_reason
    return DEFAULT_FAILURE_REASON


def resolve_exit_evidence(scene: str, *, panel_probe, result_probe,
                          story_probe=None) -> dict:
    """Copilot 结束后按证据顺序确认退出：详情面板 → 结算页 → 战后剧情 → 复检。

    实测 FC-2/3/4/5 战斗结束后停在战后剧情页：画面非战斗、既不在详情面板
    也没有结算证据，旧实现只能报 EXIT_UNCONFIRMED。这里补上剧情这一环：
    两个探针都落空时先推进战后剧情（``story_probe``），**只有它带回「画面
    已离开剧情页」的证据**才重新探测面板与结算页；剧情没推进成功就保持原有
    证据，调用方据此仍然是 unknown，绝不宣称退出。

    返回 ``{"panel": bool, "result_page": str|None, "story": dict|None}``；
    ``story`` 是 navigator.advance_post_battle_story 的结果，未尝试时为 None。
    非 ``not_battle`` 画面（活战斗/暂停/未知）一律不碰剧情，避免误点。
    """
    panel = False
    result_page = None
    story = None
    if scene != "not_battle":
        return {"panel": panel, "result_page": result_page, "story": story}
    panel = bool(panel_probe())
    if not panel:
        result_page = result_probe()
        if result_page not in RESULT_PAGE_VERDICTS and story_probe is not None:
            story = story_probe()
            if story and story.get("advanced"):
                panel = bool(panel_probe())
                if not panel:
                    result_page = result_probe()
    return {"panel": panel, "result_page": result_page, "story": story}


def story_exit_confirmed(scene: str, *, panel_confirmed: bool,
                         result_page: str | None, story: dict | None) -> bool:
    """战后剧情页被画面证据证明已离开时，战斗结束成立。

    战后剧情只会在战斗结算后播放，所以「探测确认已经不在剧情页」就是退出的
    画面证据；胜负仍以原生结算 OCR/模板为准，这里不伪造战斗结果。其余画面
    （活战斗、暂停、未知，或剧情没推进成功）一律不成立。
    """
    return (scene == "not_battle" and not panel_confirmed
            and result_page not in RESULT_PAGE_VERDICTS
            and bool(story and story.get("advanced")))


class PanelRecoveryError(RuntimeError):
    """自动归位失败；消息中携带最后截图与原生事件路径。"""


class RepositionAbortError(RuntimeError):
    """战斗前自动归位失败：结构化 ``RepositionError`` 已单独落盘。

    调用方按普通运行异常记账（``runtime_error``/``reposition_error``），但
    不再重复写一条内容相同的通用 ``RunnerError`` 事件。
    """


def _native_bindings(maa_dir: str):
    """延迟导入 MAA 绑定。"""
    sys.path.insert(0, str(Path(maa_dir) / "Python"))
    from asst.asst import Asst
    from asst.utils import InstanceOptionType
    return Asst, InstanceOptionType


def _ensure_stage_panel(stage_code: str, *, maa_dir, adb, serial, maa_user,
                        events_path) -> dict:
    """延迟导入 navigator，避免循环依赖；失败时抛出带证据路径的 RuntimeError。"""
    from src.engine.navigator import ensure_stage_panel
    try:
        return ensure_stage_panel(stage_code, maa_dir=maa_dir, adb=adb,
                                  serial=serial, maa_user=str(maa_user))
    except Exception as exc:
        raise PanelRecoveryError(
            f"自动归位到关卡 {stage_code} 详情面板失败：{exc}；"
            f"runner 事件：{events_path}") from exc


def append_runner_event(events_path, message: int, details: dict) -> None:
    """事件文件关闭后的收尾补写（如归位失败），不改写已解析的 MAA 事件。"""
    with Path(events_path).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"message": int(message),
                                 "details": json.dumps(details, ensure_ascii=False)},
                                ensure_ascii=False) + "\n")
        handle.flush()


def _advance_post_battle_story(*, maa_dir, adb, serial, maa_user) -> dict:
    """延迟导入 navigator，避免循环依赖；剧情链自己重新加载资源并连接。"""
    from src.engine.navigator import advance_post_battle_story
    return advance_post_battle_story(maa_dir=maa_dir, adb=adb, serial=serial,
                                     maa_user=str(maa_user))


def _decode_frame(image, width: int, height: int):
    """MAA 返回 PNG（实机）或 RGB/PIL（离线桩），不做 BGR 翻转。"""
    if isinstance(image, Image.Image):
        return image.convert("RGB")
    if not image:
        return None
    if image[:8] == b"\x89PNG\r\n\x1a\n":
        return Image.open(io.BytesIO(image)).convert("RGB")
    if len(image) == width * height * 3:
        return Image.frombytes("RGB", (width, height), image)
    raise ValueError(f"无法识别的 MAA 帧格式：{len(image)} 字节，期望 PNG 或 {width*height*3} 字节裸 RGB")


def _load_job(squad_path: str) -> dict:
    """读取 Copilot 作业；读取失败或结构非法时在连接前抛错。"""
    path = Path(squad_path)
    try:
        job = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"作业读取失败：{path}: {exc}") from exc
    if not isinstance(job, dict):
        raise ValueError(f"作业必须是 JSON 对象：{path}")
    if not isinstance(job.get("stage_name"), str) or not job["stage_name"].strip():
        raise ValueError(f"作业缺少 stage_name：{path}")
    if not isinstance(job.get("actions"), list):
        raise ValueError(f"作业 actions 必须是数组：{path}")
    return job


def _profile_intel_path(profile: dict):
    """按 profile 的 cache_key 直接取 intel 路径，不做难度推断。"""
    cache_key = str(profile.get("cache_key") or "")
    if not cache_key:
        raise ValueError("关卡画像缺少 cache_key")
    return ROOT / "data" / "levels" / f"{cache_key}-intel.json"


def _prepare_job(stage_code: str, squad_path: str) -> tuple[dict, dict]:
    """连接前硬门禁：作业可读、关卡画像一致、validate_job 必须通过。

    显式 stage_code 支持关卡代号或 stageId；画像一律从本地/缓存表重新解析，
    不采信调用方传入的 zero_sanity / fixed_squad / stage_profile。
    """
    job = _load_job(squad_path)
    from src.planner.intel import resolve_stage_profile

    identifier = (stage_code or "").strip() or job["stage_name"].strip()
    try:
        profile = resolve_stage_profile(identifier)
    except (KeyError, LookupError, OSError, TypeError, ValueError) as exc:
        raise ValueError(f"关卡画像解析失败：{identifier}: {exc}") from exc
    job_stage = job["stage_name"].strip()
    must_match = {str(profile.get("stage_code") or ""), str(profile.get("stage_id") or "")}
    if job_stage not in must_match and job_stage.upper() != str(profile.get("stage_code") or "").upper():
        raise ValueError(
            f"作业 stage_name {job_stage} 与请求关卡 {identifier} 不一致"
            f"（{profile.get('stage_code')}/{profile.get('stage_id')}）")
    canonical = str(profile.get("stage_code") or "").strip()
    if not canonical:
        raise ValueError(f"关卡画像缺少 stage_code：{identifier}")
    intel_path = _profile_intel_path(profile)
    if not intel_path.exists():
        from src.planner.intel import build_compact_intel
        try:
            build_compact_intel(str(profile.get("stage_id") or canonical))
        except Exception as exc:
            raise ValueError(f"关卡情报生成失败：{profile.get('stage_id')}: {exc}") from exc
    from src.engine.validator import validate_job
    validation_job = job
    stage_id = str(profile.get("stage_id") or "").strip()
    if stage_id and job_stage != stage_id:
        # 作业使用关卡代号时只为校验规范到 stageId；原始文件仍交给 Copilot。
        validation_job = {**job, "stage_name": stage_id}
    validation = validate_job(validation_job, stage_code=canonical,
                              intel_path=str(intel_path))
    if not validation.get("ok"):
        messages = [str(item.get("message") or "") for item in
                    (validation.get("errors") or [])[:3] if item.get("message")]
        reason = "; ".join(messages) or validation.get("summary") or "未知错误"
        raise ValueError(f"作业校验失败：{reason}")
    return job, profile


def run_copilot(
    stage_code: str, squad_path: str, *, mode: str = "exercise",
    outdir: str | None = None, timeout: float = 600, support: bool = False,
    maa_dir=None, adb=None, serial=None, maa_user=None,
    overlay=None, formation_index: int = 4,
    allow_leak: bool = False,
    record_video: bool = False,
) -> dict:
    """单次执行前必须先通过作业身份与 validate_job 硬门禁。

    显式 stage_code 支持关卡代号或 stageId；zero_sanity / fixed_squad 一律从
    canonical profile 派生，不接受调用方覆盖。zero_sanity 只把 exercise 升格为
    official，formation 永不开战。固定系统编队跳过 Copilot 选人；formation
    固定编队请求直接返回、不连接。
    原生参数始终 use_sanity_potion=False，禁止药水/源石回体。
    正式非零理智先跑 SanityGuardEntry；证据采样在胜负标记/链结束时冻结。
    生命传感与黑匣子共用 150ms 抓帧节拍：先以生命数字字形收敛为基线，
    连续两帧字形变化即判定扣血并冻结、原生放弃；生命点区只作 HUD 基线条件，
    并随证据记录前后生命签名。
    allow_leak=True 只记录首次漏怪，不中断 Copilot。
    漏怪/超时/战斗中异常先停止自动化，再走原生放弃链（先确认暂停，再点左上
    设置，再点原生「放弃行动」确认）。退出判定用右上控制块的暂停/播放模板
    分类画面：暂停菜单、选中干员遮罩、生命点消失都不算退出；只有原生
    MarkBattleAbandoned 加上非战斗画面才允许普通导航，画面未知时按失败处理
    并保留最后截图，放弃失败绝不报成功。
    record_video=True 时用同一 monotonic 时钟记录 screenrecord 启动（视频 0 秒）
    与 Copilot append/start 成功（战斗 0 秒），并写出 review-manifest.json；
    录像未开启或拉取失败时只记录 warnings，不伪造视频路径。
    """
    if mode not in MODE_OVERLAY:
        raise ValueError(f"不支持的运行模式：{mode!r}；应为 exercise/official/formation")
    _job, profile = _prepare_job(stage_code, squad_path)
    stage_code = profile["stage_code"]
    cache_key = str(profile.get("cache_key") or "")
    zero_sanity = bool(profile["zero_sanity"])
    fixed_squad = bool(profile["fixed_squad"])
    if zero_sanity and mode == "exercise":
        mode = "official"
    allow_official = mode == "official"
    formation_no_battle = mode == "formation" and fixed_squad
    maa_dir = str(maa_dir or DEFAULT_MAA)
    live_template = Path(maa_dir) / "resource" / "template" / BATTLE_PAUSE_BUTTON
    paused_template = ABANDON_BATTLE / "resource" / "template" / BATTLE_PAUSE_MENU
    if mode != "formation":
        for path in (live_template, paused_template):
            if not path.is_file():
                raise FileNotFoundError(f"缺少实机识别模板：{path}；参照 README 的本地暂停模板设置。")
            with Image.open(path, formats=("PNG",)) as template:
                if template.width > BATTLE_CONTROL_ROI[2] or template.height > BATTLE_CONTROL_ROI[3]:
                    raise ValueError(f"实机识别模板大于战斗控件区域：{path}")
                template.load()
    adb = str(adb or DEFAULT_ADB)
    serial = serial or DEFAULT_SERIAL
    video_proc = None
    video_remote = None
    overlay = Path(overlay) if overlay else ROOT / "tools" / "maa-resources" / MODE_OVERLAY[mode]
    maa_user = Path(maa_user or ROOT / "data" / "maa_userdata").resolve()
    if outdir is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        outdir = str(ROOT / "data" / "captures" / f"{stage_code}-{mode}-{stamp}")
    output_dir = Path(outdir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    maa_user.mkdir(parents=True, exist_ok=True)
    events_path = output_dir / "maa-events.jsonl"
    report_path = output_dir / "maa-run.json"
    manifest_path = output_dir / REVIEW_MANIFEST_NAME
    params = {
        "filename": str(Path(squad_path).resolve()),
        "formation": not fixed_squad,
        "formation_index": formation_index,
        "ignore_requirements": True,
        "add_trust": False,
        "support_unit_usage": 1 if support else 0,
        "loop_times": 1,
        "use_sanity_potion": False,
    }
    # 优先用 MAA Tile-Pos 的视口参数做等距投影网格；缺失时才退回逻辑网格。
    tile_grid = load_projection_meta(cache_key, maa_dir=maa_dir)
    if tile_grid is None:
        intel_path = _profile_intel_path(profile)
        if intel_path.exists():
            tile_grid = json.loads(intel_path.read_text(encoding="utf-8"))["tiles"]
    life_total = None
    level_path = ROOT / "data" / "levels" / f"{cache_key}.json"
    if level_path.exists():
        level = json.loads(level_path.read_text(encoding="utf-8"))
        life_total = level["options"]["maxLifePoint"]
    frames: deque = deque(maxlen=24)
    native_lock = threading.Lock()
    events_lock = threading.Lock()
    frames_lock = threading.Lock()
    stop_sampling = threading.Event()
    sampling_errors: list[str] = []
    callback_errors: list[str] = []
    chains: set[str] = set()
    battle_marks: set[str] = set()
    win_evidence: str | None = None  # MarkBattleWon 的三星复核结论：verified/rejected/unverified
    benign_taskids: set[int] = set()
    panel_marks: set[str] = set()
    action_seen = [0]  # 已执行动作计数（零动作说明战斗根本没开始）  # 结果探测链的「未命中」是预期结果，不是失败
    sanity_marks: set[str] = set()
    guard_chain_errors: list[str] = []
    sanity_guard = "ok"
    guard_pending = False
    resolution = (1280, 720)
    asst = None
    sampler = None
    task_id = None
    runtime_error = None
    cleanup_errors: list[str] = []
    timed_out = False
    leak: dict | None = None
    leak_evidence_png: str | None = None
    leak_evidence_frames: list[dict] | None = None  # 四宫格每格的真实采样时间
    leak_evidence_attempted = False  # 拼图只尝试一次：失败不重复生成事件/孤儿 PNG
    leak_evidence_error: str | None = None  # 上一次（也是唯一一次）拼图失败原因
    leak_pin_frame = None  # 首扣血现场（冻结前最后一帧仍稳定的画面）
    result_page: str | None = None  # 原生结算页证据：failed / won
    exit_unconfirmed = False  # 退出未确认：保守处理并保留画面
    auto_reposition_pending = False
    copilot_stopped = False
    abandon_failed = False
    abandon_marks: set[str] = set()
    abandoned = False
    abandon_reason: str | None = None
    exit_confirmed = False
    reposition_error: str | None = None  # 归位失败事实；不影响战报落盘
    last_screenshot = None
    t0 = time.monotonic()
    timeline = ReviewTimeline()
    video_started = False
    video_pulled = False
    events = events_path.open("w", encoding="utf-8")

    def write_event(message: int, details: dict) -> None:
        with events_lock:
            events.write(json.dumps({"message": message,
                                     "details": json.dumps(details, ensure_ascii=False)},
                                    ensure_ascii=False) + "\n")
            events.flush()

    def _callback(message, details, _arg):
        nonlocal resolution
        # ctypes 不会将回调异常传回主线程，必须显式记录并让主循环终止。
        try:
            text = details.decode("utf-8", "replace") if details else "{}"
            parsed = json.loads(text)
            details_out = text
            if parsed.get("what") == "CopilotAction":
                action_seen[0] += 1
                # 只改 runner 自己写出的副本；MAA 原生日志不动。原始 elapsed_time
                # 有效时保留，-1/缺失时用本地单调时钟补成战斗秒。
                if _fill_elapsed_time(parsed, timeline.battle_s()):
                    details_out = json.dumps(parsed, ensure_ascii=False)
            task_name = (parsed.get("details") or {}).get("task")
            if task_name in {"MarkBattleFailed", "MarkBattleWon"}:
                with frames_lock:
                    stop_sampling.set()
                battle_marks.add(task_name)
            if task_name in {"MarkBattleAbandoned", "MarkAbandonFailed",
                             "MarkAbandonActionEnded"}:
                abandon_marks.add(task_name)
            if task_name in {"MarkSanityExhausted", "MarkSanityOk"}:
                sanity_marks.add(task_name)
            if task_name in {"MarkAtPanel", "MarkNotAtPanel"}:
                panel_marks.add(task_name)
            if guard_pending and int(message) in {10000, 20000}:
                guard_chain_errors.append(text)
            # 结果探测链「没识别到关键字」会以 TaskChainError 结束，属预期语义；
            # 降级为 -3 诊断事件，避免把 battle_result=won 整局误判为 failed。
            if int(message) in (10000, 20000) and parsed.get("taskid") in benign_taskids:
                message = -3
            with events_lock:
                events.write(json.dumps({"message": int(message), "details": details_out},
                                        ensure_ascii=False) + "\n")
                events.flush()
            if parsed.get("taskchain"):
                chains.add(parsed["taskchain"])
            if parsed.get("what") in {"ResolutionGot", "ResolutionInfo"}:
                data = parsed["details"]
                resolution = (int(data["width"]), int(data["height"]))
        except Exception as exc:
            callback_errors.append(f"MAA 回调异常：{type(exc).__name__}: {exc}")

    def sample_frames() -> None:
        """持续采样直到退出决策；Copilot 链结束不等于战斗结束。

        旧实现在 Copilot 链一结束就冻结采样（实测 40s 动作后就停），战斗到
        75s 才失败，后面的扣血完全看不到。这里只在漏怪确认或整体收尾时停。
        """
        nonlocal leak, copilot_stopped, leak_pin_frame
        leak_sampler = LeakSampler()
        frame_index = -1
        try:
            while not stop_sampling.is_set():
                tick = time.monotonic()
                with native_lock:
                    if stop_sampling.is_set():
                        break
                    width, height = resolution
                    image = asst.get_image(width * height * 3)
                    # get_image 可与原生回调交错；冻结期间返回的帧必须丢弃。
                    if stop_sampling.is_set():
                        break
                frame = _decode_frame(image, width, height)
                # 采样时刻在本帧解码完成时取一次，三个锚点共用同一时钟读数。
                sampled_at = time.monotonic()
                confirmed = False
                if frame is not None:
                    with frames_lock:
                        if stop_sampling.is_set():
                            break
                        frame_index += 1
                        frame.info["sampled_after_freeze"] = False
                        frame.info["frame_index"] = frame_index
                        frame.info.update(timeline.sample_stamp(sampled_at))
                        frames.append(frame)
                        # 共用刚抓到的图，不为传感额外请求 get_image。
                        fact = leak_sampler.observe(frame, frame_index=frame_index)
                        if fact is not None and leak is None:
                            timeline.mark_leak()
                            leak = {
                                **fact,
                                "life_total": life_total, "estimated": life_total is None,
                                "battle_s": (None if timeline.leak_battle_s() is None
                                             else round(timeline.leak_battle_s(), 3)),
                                "video_s": (None if timeline.leak_video_s() is None
                                            else round(timeline.leak_video_s(), 3)),
                            }
                            # 证据要保留首扣血现场（比确认时刻早一个采样节拍）。
                            leak_pin_frame = leak_sampler.first_change_frame
                            confirmed = True
                            if not allow_leak:
                                stop_sampling.set()
                    if confirmed:
                        if not allow_leak:
                            if task_id is not None:
                                # 主动中断会让当前 Copilot 链以 TaskChainError 收尾：
                                # 这是预期结果，不能把已确认的漏怪局改判 failed。
                                benign_taskids.add(int(task_id))
                            with native_lock:
                                copilot_stopped = True
                                if not asst.stop():
                                    raise RuntimeError("漏怪中断 Copilot 失败：MAA stop 返回失败")
                        write_event(-2, {"what": "LeakDetected", "details": leak})
                # get_image 通常需 30–80ms；150ms 总节拍避免占满原生锁。
                stop_sampling.wait(max(0.0, 0.15 - (time.monotonic() - tick)))
        except Exception as exc:
            sampling_errors.append(f"帧采样异常：{type(exc).__name__}: {exc}")
            stop_sampling.set()

    def _stop_automation() -> None:
        """停止当前 MAA 链；重复调用不重复 stop 已停实例。"""
        nonlocal copilot_stopped
        if asst is None:
            raise RuntimeError("MAA 实例不存在，无法停止自动化")
        with native_lock:
            if not copilot_stopped or asst.running():
                if task_id is not None:
                    # 主动停止后 MAA 会用 TaskChainError 结束当前 Copilot 链；
                    # 登记为预期语义，自然失败不会走到这里，仍保持 failed。
                    benign_taskids.add(int(task_id))
                if not asst.stop():
                    raise RuntimeError("停止 Copilot 自动化失败")
                copilot_stopped = True

    def _battle_scene_state(attempts: int = 1, delay: float = 0.4):
        """抓帧分类战斗画面，返回 (scene, frame)。

        只看生命点会把暂停/选中干员/压暗的活战斗误判成已退出；``unknown``
        表示抓帧或识别失败，调用方绝不能把它当安全退出。抓帧异常在此吸收，
        否则会中断放弃流程。
        """
        scene = "unknown"
        frame = None
        rounds = max(1, attempts)
        for index in range(rounds):
            if asst is None:
                return "unknown", None
            try:
                with native_lock:
                    image = asst.get_image(resolution[0] * resolution[1] * 3)
                frame = _decode_frame(image, *resolution)
                scene = battle_scene_state(frame, live_template=live_template,
                                           paused_template=paused_template)
            except Exception as exc:
                frame = None
                scene = "unknown"
                write_event(-1, {"what": "RunnerError",
                                 "why": f"战斗画面探针失败：{type(exc).__name__}: {exc}"})
            if scene != "unknown" or index == rounds - 1:
                break
            time.sleep(delay)
        return scene, frame

    def _panel_confirmed() -> bool:
        """用原生 BattleReadyCheck 确认当前在关卡详情面板。

        等链结束的整段时间都不持 native_lock：帧采样线程要在这段时间继续抓图，
        否则退出确认的十几秒里漏怪又被看不见了。
        """
        panel_marks.clear()
        with native_lock:
            probe_id = asst.append_task("Custom", {"task_names": ["BattleReadyCheck"]})
            if not probe_id:
                raise RuntimeError("MAA 退出确认任务添加失败")
            benign_taskids.add(probe_id)
            if not asst.start():
                raise RuntimeError("MAA 退出确认启动失败")
        deadline = time.monotonic() + 15
        while True:
            with native_lock:
                if not asst.running():
                    break
                if time.monotonic() > deadline:
                    asst.stop()
                    return False
            time.sleep(0.1)
        confirmed = "MarkAtPanel" in panel_marks and "MarkNotAtPanel" not in panel_marks
        write_event(2, {"what": "ExitConfirm", "panel": confirmed})
        return confirmed

    def _run_custom_probe(probe_task: str, timeout_s: float,
                          *, label: str | None = None) -> None:
        """跑一条只读 Custom 链并等它结束；未命中/超时都只是「没拿到证据」。

        ``label`` 只供放弃复核路径使用：把真实 append 的 taskid、超时和启动
        失败写进事件，便于复盘「只跑了一条探测链」这类时序问题。
        """
        with native_lock:
            probe_id = asst.append_task("Custom", {"task_names": [probe_task]})
            if not probe_id:
                raise RuntimeError(f"MAA 结果探测任务添加失败：{probe_task}")
            benign_taskids.add(probe_id)
            if not asst.start():
                raise RuntimeError(f"MAA 结果探测启动失败：{probe_task}")
        if label is not None:
            write_event(2, {"what": "AbandonProbeAppend", "reason": label,
                            "task": probe_task, "taskid": probe_id,
                            "timeout_s": timeout_s})
        probe_deadline = time.monotonic() + timeout_s
        timed_out = False
        while True:
            with native_lock:
                if not asst.running():
                    break
                if time.monotonic() > probe_deadline:
                    asst.stop()
                    timed_out = True
                    break
            time.sleep(0.1)
        if timed_out and label is not None:
            write_event(-2, {"what": "AbandonProbeTimeout", "reason": label,
                             "task": probe_task, "taskid": probe_id})

    def _won_verdict() -> str:
        """``MarkBattleWon`` 只说明结算页出现：必须有当前帧三星奖章佐证。

        只在 ``not_battle`` + 有效帧上做三星判定（``won_evidence_verdict``）：

          - ``verified`` → ``won``；
          - ``rejected``（画面已离开战斗但没有三枚蓝章）→ 划掉标记，``ended``；
          - ``unverified``（抓帧失败/画面未知/仍在战斗）→ **保留**原生标记，
            只返回 ``ended``（标记本身只证明结算页出现），并记下
            ``win_evidence="unverified"``；最终战报由
            ``verified_native_battle`` 拒绝把未复核的标记当胜利。

        ``unknown`` 会先重试几轮抓帧再下结论；这既不会永久销毁真实胜利，
        也不会把通用结算页（行动结束/行动结果）误报成 won。
        """
        nonlocal win_evidence
        scene, frame = "unknown", None
        for attempt in range(3):
            try:
                scene, frame = _battle_scene_state(attempts=2, delay=0.3)
            except Exception as exc:  # _battle_scene_state 内部已吸收抓帧异常
                scene, frame = "unknown", None
                write_event(-1, {"what": "RunnerError",
                                 "why": (f"三星奖章复检抓帧失败："
                                         f"{type(exc).__name__}: {exc}")})
            # not_battle 才可能读到奖章；live/paused 已足以说明结论未定，
            # 只有 unknown/抓帧失败值得再等一拍重试。
            if scene in {"not_battle", "live", "paused"} or attempt == 2:
                break
            time.sleep(0.5)
        three_star = (result_three_star(frame)
                      if scene == "not_battle" and frame is not None else False)
        verdict = won_evidence_verdict(scene, frame_present=frame is not None,
                                       three_star=three_star)
        write_event(2, {"what": "BattleWonEvidence", "verdict": verdict,
                        "three_star": three_star, "scene": scene,
                        "frame": frame is not None})
        if verdict == "verified":
            win_evidence = "verified"
            return "won"
        if verdict == "rejected":
            win_evidence = "rejected"
            battle_marks.discard("MarkBattleWon")
            write_event(-2, {
                "what": "BattleWonUnproven", "three_star": False,
                "scene": scene, "frame": True,
                "why": ("原生 MarkBattleWon 未获三星奖章佐证：只认定战斗结束"
                        "（ended），不判胜利")})
            return "ended"
        # unverified：保留 MarkBattleWon（真实胜利不能被一次抓帧失败永久
        # 丢掉），但绝不据它判胜；失败只影响胜负，不阻碍退出确认。
        win_evidence = "unverified"
        write_event(-2, {
            "what": "BattleWonUnverified", "three_star": False,
            "scene": scene, "frame": frame is not None,
            "why": ("原生 MarkBattleWon 未能在当前帧复核（抓帧失败/画面未知/"
                    "仍在战斗）：保留胜利标记待复核，本次不判胜利")})
        return "ended"

    def _result_page_probe() -> str | None:
        """原生结算页复检：任务失败/作战成功 OCR 命中才算战斗结束。

        Copilot 完成时战斗通常还没结束（实测 40s 动作完成、74s 才出失败页），
        第一次结果探测必然落空；退出决策时再问一次。返回 ``failed``/``won``/
        ``ended``，未命中返回 None——此时绝不能靠「右上控制块消失」推断结算。

        ``ResultWinCheck`` 的 ``MarkBattleWon`` 只是「结算页出现」：1/2 星和
        通用「行动结束/行动结果」都会命中。这里必须再抓当前帧验证三枚蓝色
        奖章：画面已离开战斗却没有奖章才划掉胜利标记并降级为 ``ended``；
        抓帧失败/画面未知时保留标记，但同样只按 ``ended`` 退出（最终胜负由
        ``verified_native_battle`` 拒绝未复核的标记）。
        """
        for probe_task, mark, verdict in (
                ("ResultFailedCheck", "MarkBattleFailed", "failed"),
                ("ResultWinCheck", "MarkBattleWon", "won")):
            if mark not in battle_marks:
                _run_custom_probe(probe_task, 20)
            if mark not in battle_marks:
                continue
            return _won_verdict() if verdict == "won" else verdict
        return None

    def _abandon_and_confirm(reason: str) -> str:
        """停止自动化后驱动原生放弃链。

        返回 ``abandoned``（原生 MarkBattleAbandoned + 离线退出裁决都成立）、
        ``result_page``（链在盲点暂停前就用原生 OCR 认出任务失败页，战斗其实
        已经结束，不需要放弃）、``ended``（链认出通用结算页标题「行动结束/
        MISSION RESULTS」，战斗确实已结束，但这不是胜负证据）或 ``failed``
        （调用方按放弃失败处理）。

        超时/异常/停止失败也会先看 ``abandon_marks``：链可能已经写下结算
        证据，这类失败不能盖掉「战斗已结束」。但结算证据还必须再过一次
        ``_battle_scene_state`` 复核，只有 ``not_battle`` 才允许调用方确认
        退出；``live``/``paused``/``unknown`` 返回 ``failed``，保留人工处理。

        链自身失败（``MarkAbandonFailed`` 或没有 ``MarkBattleAbandoned``）时
        再多查一次结算页：实测放弃链早于「行动结束」渲染启动，结算页上没有
        暂停按钮，链会直接失败，而画面随后才停在「任务失败/行动结果」。复查
        会先等渲染，再**先**跑 ``ResultWinCheck``（命中只作 ``ended``）、未命中
        才跑 ``ResultFailedCheck``（命中作 ``result_page``），两者都要过画面
        复核；只有都探测不到才返回 ``failed``。``won`` 在放弃路径上只用于
        退出，不判胜负。
        """
        if asst is None:
            return "failed"

        def _guard_exit(reason: str, guard: str,
                        *, native: str | None = None) -> str:
            """结算证据的退出复核：OCR 之后必须再看一帧是否真的离开战斗。"""
            scene, frame = _battle_scene_state(attempts=3, delay=0.5)
            hud = is_battle_hud(frame) if frame is not None else None
            allowed = ended_exit_allowed(scene)
            write_event(2, {
                "what": ("AbandonResultPage" if guard == "result_page"
                         else "AbandonActionEnded"),
                "reason": reason, "guard": guard, "scene": scene, "hud": hud,
                "frame": frame is not None, "exit_allowed": allowed,
                "native": native or (
                    "MarkAbandonResultPage" if guard == "result_page"
                    else "MarkAbandonActionEnded")})
            if not allowed:
                # 画面还在战斗/暂停或未知：结算证据不足以确认退出。
                write_event(-1, {
                    "what": "RunnerError",
                    "why": (f"{reason} 放弃链已见结算证据（{guard}），但退出复核"
                            f" scene={scene}, hud={hud}：不确认退出，保留人工处理")})
                return "failed"
            return guard

        result_probed = False

        def _probe_idle(reason: str, next_task: str) -> None:
            """探测之间确认上一条链真的停了：MAA 拒绝在运行中 append。"""
            with native_lock:
                running = asst.running()
                stopped = True if not running else asst.stop()
            if running:
                write_event(2, {"what": "AbandonProbeStop", "reason": reason,
                                "next": next_task, "stopped": bool(stopped)})
                if not stopped:
                    raise RuntimeError(f"结算页复查前停止旧链失败：{next_task}")
            time.sleep(ABANDON_PROBE_GAP)

        def _result_page_after_abandon(reason: str) -> str | None:
            """放弃链没给出退出证据时，先等结算页渲染，再用只读探测复查。

            实测（真机验收 A2）放弃链在「行动结束」渲染完成前就启动：
            ``AbandonEntry`` 盲点 ``BattlePause`` 失败后直接写
            ``MarkAbandonFailed``，此时立刻复用失败优先的
            ``_result_page_probe`` 只会跑一条 ``ResultFailedCheck``——未命中，
            第二次 ``ResultWinCheck`` 还常因上一条链没停而 append 被拒。等
            2.5s 再单独重放 ``ResultWinCheck`` 则稳稳命中「行动结束」0.996。

            所以这里不复用 ``_result_page_probe`` 的顺序：
              1. 先等 ``ABANDON_RESULT_RENDER_WAIT`` 秒让结算页画出来；
              2. 先跑 ``ResultWinCheck``（25s）；命中后用 ``_won_verdict``
                 复核当前帧奖章，但**只**返回 ``ended``（退出），绝不判胜；
              3. 未命中才跑 ``ResultFailedCheck``（20s），命中按 ``result_page``；
              4. 两次探测之间先确认上一条链已停并留短间隔；每次 append、
                 超时、命中都写事件，便于复盘只发一条链的时序问题。
            返回 ``result_page``/``ended``；None 表示探测不到或探测本身失败，
            调用方仍按放弃失败处理。每个放弃流程只探测一次。
            """
            nonlocal result_probed
            if result_probed or asst is None:
                return None
            result_probed = True
            write_event(2, {"what": "AbandonResultProbeWait", "reason": reason,
                            "wait_s": ABANDON_RESULT_RENDER_WAIT})
            time.sleep(ABANDON_RESULT_RENDER_WAIT)
            probes = (
                ("ResultWinCheck", "MarkBattleWon",
                 ABANDON_WIN_PROBE_TIMEOUT),
                ("ResultFailedCheck", "MarkBattleFailed",
                 ABANDON_FAILED_PROBE_TIMEOUT),
            )
            for probe_task, mark, timeout_s in probes:
                try:
                    _probe_idle(reason, probe_task)
                    if mark not in battle_marks:
                        _run_custom_probe(probe_task, timeout_s, label=reason)
                except Exception as exc:
                    write_event(-1, {"what": "RunnerError",
                                     "why": (f"{reason} 放弃后结算页复查失败"
                                             f"（{probe_task}）："
                                             f"{type(exc).__name__}: {exc}")})
                    return None
                if mark not in battle_marks:
                    continue
                write_event(2, {"what": "AbandonProbeHit", "reason": reason,
                                "task": probe_task, "mark": mark})
                if probe_task == "ResultWinCheck":
                    # MarkBattleWon 只证明结算页出现（行动结束/行动结果都会
                    # 命中）；_won_verdict 复核奖章，但放弃路径只作退出。
                    _won_verdict()
                    return _guard_exit(reason, "ended",
                                       native="MarkBattleWon")
                return _guard_exit(reason, "result_page",
                                   native="MarkBattleFailed")
            return None

        try:
            abandon_marks.clear()
            _stop_automation()
            with native_lock:
                if not Asst.load(str(ABANDON_BATTLE)):
                    raise RuntimeError("MAA 放弃行动资源加载失败")
                if not asst.append_task("Custom", {"task_names": ["AbandonEntry"]}):
                    raise RuntimeError("MAA 放弃行动任务添加失败")
                if not asst.start():
                    raise RuntimeError("MAA 放弃行动启动失败")
            deadline = time.monotonic() + 90
            while True:
                with native_lock:
                    abandoning = asst.running()
                if not abandoning:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("MAA 放弃行动超时")
                time.sleep(0.1)
            guard_verdict = abandon_result_verdict(abandon_marks)
            # 失败页/结算页上点暂停没有任何意义：链已在盲点暂停前停下。
            # 但只有退出复核（画面 not_battle）通过才允许调用方确认退出。
            if guard_verdict is not None:
                return _guard_exit(reason, guard_verdict)
            if ("MarkBattleAbandoned" not in abandon_marks
                    or "MarkAbandonFailed" in abandon_marks):
                # 放弃链自身失败（实测：结算页上点 BattlePause 必败，随后直接
                # 落 MarkAbandonFailed）。画面可能早已离开战斗，先复查结算
                # OCR；命中就按结算证据退出，只有探测不到才抛给兜底置 failed。
                probed = _result_page_after_abandon(reason)
                if probed is not None:
                    return probed
                raise RuntimeError("原生放弃链未返回 MarkBattleAbandoned")
            verify_deadline = time.monotonic() + ABANDON_HUD_TIMEOUT
            attempt = 0
            scene, hud = "unknown", None
            while time.monotonic() < verify_deadline:
                attempt += 1
                scene, frame = _battle_scene_state()
                hud = is_battle_hud(frame) if frame is not None else None
                verdict = battle_exit_verdict(scene, abandon_marks)
                write_event(2, {"what": "AbandonVerify", "reason": reason,
                                "attempt": attempt, "frame": frame is not None,
                                "scene": scene, "hud": hud, "verdict": verdict,
                                "native": "MarkBattleAbandoned"})
                if frame is not None and attempt == 1:
                    frame.save(output_dir / "abandon-verify.png", format="PNG")
                if verdict == "exited":
                    return "abandoned"
                time.sleep(0.5)
            raise RuntimeError(
                f"放弃后仍无退出证据（scene={scene}, hud={hud}）："
                "原生标记或画面状态未知，不能当已退出")
        except Exception as exc:
            write_event(-1, {"what": "RunnerError",
                             "why": f"{reason} 放弃失败：{type(exc).__name__}: {exc}"})
            # 超时/异常/停止失败：链可能已经写下结算证据，不能直接盖成
            # failed；复核对画面负责，只有 not_battle 才返回 result_page/ended。
            guard_verdict = abandon_result_verdict(abandon_marks)
            if guard_verdict is not None:
                return _guard_exit(reason, guard_verdict)
            # 链在结算页上失败时不会写 MarkAbandonResultPage：
            # 再问一次原生结果 OCR，命中同样按结算证据退出。
            probed = _result_page_after_abandon(reason)
            if probed is not None:
                return probed
            return "failed"

    def _capture_leak_evidence() -> bool:
        """冻结环 → 2×2 四宫格 → 同名 sidecar → LeakEvidence 事件。

        拼图与 sidecar 是两个独立失败点：sidecar 写失败只记 sidecar_error，
        PNG 成功时事件照写（timeline 为 null），复核时至少还拿得到图；拼图
        失败才没有事件可指。两种失败都进 cleanup_errors，绝不阻断放弃；
        也不承诺返还理智——正式模式失败是否返还由游戏规则决定，本仓库未验证。
        拼图只尝试一次：失败原因记在 leak_evidence_error，重复调用直接返回
        False，避免重复事件、重复 cleanup_errors 与孤儿 PNG。
        返回四宫格 PNG 是否已落盘。
        """
        nonlocal leak_evidence_png, leak_evidence_frames
        nonlocal leak_evidence_attempted, leak_evidence_error
        if leak_evidence_png is not None:
            return True
        if leak_evidence_attempted:
            # 拼图只尝试一次：重复失败会写出重复的 RunnerWarning/cleanup_errors，
            # 还会在 sidecar 之前留下更多孤儿 PNG。
            return False
        leak_evidence_attempted = True
        try:
            with frames_lock:
                ring = list(frames)
            chosen = select_evidence_frames(ring, need=4, include_last=True)
            if not chosen:
                raise RuntimeError("冻结环中没有可用证据帧")
            chosen = evidence_with_pin(chosen, leak_pin_frame)
            leak_evidence_frames = evidence_frame_meta(
                chosen, pin=leak_pin_frame)
            leak_evidence_png = str(compose_evidence_grid(
                chosen, stage_code=stage_code,
                out_path=output_dir / (
                    f"leak-evidence-{datetime.now():%Y%m%d-%H%M%S-%f}.png"),
                tile_grid=tile_grid))
        except Exception as exc:
            why = f"漏怪证据拼图失败：{type(exc).__name__}: {exc}"
            leak_evidence_error = why
            # 只进 cleanup_errors：收尾统一落一条 RunnerWarning（不是
            # RunnerError），避免同一失败在捕获时与 flush 时各写一遍。
            cleanup_errors.append(why)
            return False
        # 与四宫格同名的 sidecar：每格的真实采样时间 + 传感事实，
        # 复核时按 sampled_at 定位视频，而不必相信帧序号。
        sidecar_path = Path(leak_evidence_png).with_suffix(".json")
        sidecar_error = None
        try:
            sidecar_path.write_text(json.dumps(
                leak_evidence_payload(
                    png=leak_evidence_png, stage_code=stage_code,
                    leak=leak, frames=leak_evidence_frames),
                ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            sidecar_error = f"{type(exc).__name__}: {exc}"
            why = f"漏怪证据 sidecar 写入失败：{sidecar_error}"
            leak_evidence_error = why
            cleanup_errors.append(why)
        event = {
            "what": "LeakEvidence",
            "path": leak_evidence_png,
            "timeline": None if sidecar_error else str(sidecar_path),
            "frames": leak_evidence_frames,
        }
        if sidecar_error is not None:
            event["sidecar_error"] = sidecar_error
        write_event(2, event)
        return True

    try:
        Asst = None
        InstanceOptionType = None
        copilot_started = False
        if not formation_no_battle:
            Asst, InstanceOptionType = _native_bindings(maa_dir)
            callback = Asst.CallBackType(_callback)

            def prepare_instance():
                """加载基础资源、模式/探测 overlay 并连接；归位后必须重新准备。"""
                with native_lock:
                    if not Asst.load(maa_dir, user_dir=str(maa_user)):
                        raise RuntimeError(f"MAA 基础资源加载失败：{maa_dir}")
                    if not Asst.load(str(overlay)):
                        raise RuntimeError(f"MAA overlay 加载失败：{overlay}")
                    official_overlay = ROOT / "tools" / "maa-resources" / MODE_OVERLAY["official"]
                    if mode == "official" and overlay.resolve() != official_overlay.resolve():
                        if not Asst.load(str(official_overlay)):
                            raise RuntimeError(f"MAA 理智安全资源加载失败：{official_overlay}")
                    # 结果探测资源与模式 overlay 叠加：Copilot 链正常结束不代表打赢。
                    if not Asst.load(str(RESULT_PROBE)):
                        raise RuntimeError(f"MAA 结果探测资源加载失败：{RESULT_PROBE}")
                    if not Asst.load(str(PREFLIGHT)):
                        raise RuntimeError(f"MAA 前置检查资源加载失败：{PREFLIGHT}")
                    instance = Asst(callback=callback)
                    if not instance.set_instance_option(InstanceOptionType.touch_type, "maatouch"):
                        raise RuntimeError("设置 MaaTouch 失败")
                    if not instance.connect(adb, serial, "MuMuEmulator12"):
                        raise RuntimeError("MAA 连接失败")
                    return instance

            try:
                asst = prepare_instance()
                if mode == "official" and not zero_sanity:
                    guard_pending = True
                    with native_lock:
                        if not asst.append_task("Custom", {"task_names": ["SanityGuardEntry"]}):
                            raise RuntimeError("MAA 理智检查任务添加失败")
                        if not asst.start():
                            raise RuntimeError("MAA 理智检查启动失败")
                    guard_deadline = time.monotonic() + min(timeout, 25)
                    while True:
                        if callback_errors:
                            raise RuntimeError("; ".join(callback_errors))
                        with native_lock:
                            running = asst.running()
                        if not running:
                            break
                        if time.monotonic() >= guard_deadline:
                            raise RuntimeError("MAA 理智检查超时")
                        time.sleep(0.1)
                    if callback_errors or guard_chain_errors:
                        raise RuntimeError("; ".join(callback_errors + guard_chain_errors))
                    if "MarkSanityExhausted" in sanity_marks:
                        sanity_guard = "blocked"
                    elif "MarkSanityOk" not in sanity_marks:
                        raise RuntimeError("MAA 理智检查链未返回 MarkSanityExhausted/MarkSanityOk")
                    guard_pending = False

                if sanity_guard != "blocked":
                    with native_lock:
                        # 前置检查：必须真的停在关卡详情面板（有「开始行动/演习」），
                        # 否则会像实测那样在结算页上启动，白跑一次 official。
                        preflight_id = asst.append_task("Custom", {"task_names": ["BattleReadyCheck"]})
                        if not preflight_id:
                            raise RuntimeError("MAA 前置检查任务添加失败")
                        benign_taskids.add(preflight_id)
                        if not asst.start():
                            raise RuntimeError("MAA 前置检查启动失败")
                    preflight_deadline = time.monotonic() + 25
                    while True:
                        with native_lock:
                            preflight_running = asst.running()
                        if not preflight_running:
                            break
                        if time.monotonic() > preflight_deadline:
                            with native_lock:
                                asst.stop()
                            raise RuntimeError("MAA 前置检查超时")
                        time.sleep(0.1)
                    if "MarkNotAtPanel" in panel_marks or "MarkAtPanel" not in panel_marks:
                        # 不在详情面板：释放当前实例（导航链会重新加载基础资源），
                        # 归位成功后重建同一执行实例，继续本次 Copilot。
                        write_event(-2, {"what": "AutoReposition",
                                         "why": "前置检查未确认关卡详情面板"})
                        with native_lock:
                            if not asst.stop():
                                raise RuntimeError("自动归位前停止 MAA 实例失败")
                        asst = None
                        try:
                            _ensure_stage_panel(stage_code, maa_dir=maa_dir, adb=adb,
                                                serial=serial, maa_user=str(maa_user),
                                                events_path=events_path)
                        except Exception as exc:
                            # 归位失败只记为事实：本次战斗无法安全开始，但收尾
                            # 必须走到战报落盘，不能让异常吞掉 result/maa-run.json。
                            reposition_error = f"{type(exc).__name__}: {exc}"
                            write_event(-1, {"what": "RepositionError",
                                             "why": reposition_error})
                            raise RepositionAbortError(
                                f"战斗前自动归位失败：{reposition_error}") from exc
                        asst = prepare_instance()
                        panel_marks.clear()
                    video_proc = None
                    video_remote = None
                    if record_video:
                        video_remote = f"/sdcard/maa_{int(time.time())}.mp4"
                        video_proc = subprocess.Popen(
                            [adb, "-s", serial, "shell", "screenrecord", "--time-limit", "900", video_remote],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        video_started = True
                        # 进程启动时刻即 battle.mp4 的 0 秒；下面 1.5s 属于录像内的
                        # 准备时间，之后换算出的 battle_start 会如实包含它。
                        timeline.mark_video_start()
                        time.sleep(1.5)
                    with native_lock:
                        task_id = asst.append_task("Copilot", params)
                        if not task_id:
                            raise RuntimeError("MAA append_task 失败")
                        if not asst.start():
                            raise RuntimeError("MAA start 失败")
                    # append/start 成功、采样线程启动前：本地战斗秒的 0 点。
                    timeline.mark_battle_start()
                    copilot_started = True
                    auto_reposition_pending = True
                    sampler = threading.Thread(target=sample_frames, name="maa-frame-sampler", daemon=True)
                    sampler.start()
                    while True:
                        if sampling_errors or callback_errors:
                            raise RuntimeError("; ".join(sampling_errors + callback_errors))
                        with native_lock:
                            running = asst.running()
                        # Copilot 链结束不等于战斗结束：采样要活到退出决策，
                        # 否则战斗后段的扣血（实测 2→1 在 72s）永远看不到。
                        if not running:
                            break
                        if time.monotonic() - t0 >= timeout:
                            timed_out = True
                            stop_sampling.set()
                            write_event(-1, {"what": "RunnerTimeout", "why": f"执行超时（{timeout}s）"})
                            break
                        time.sleep(0.1)
            except Exception as exc:
                if guard_pending:
                    raise
                if isinstance(exc, RepositionAbortError):
                    # 归位失败已作为独立事实记入 reposition_error 与
                    # RepositionError 事件；不再重复算成通用运行异常，否则
                    # program_status 会落成 failed 而不是 REPOSITION_FAILED。
                    runtime_error = None
                else:
                    runtime_error = f"{type(exc).__name__}: {exc}"
                    write_event(-1, {"what": "RunnerError", "why": runtime_error})
            battle_proven = leak is not None and not allow_leak
            if (mode != "formation" and copilot_started and not battle_proven
                    and (timed_out or runtime_error is not None)):
                try:
                    _stop_automation()
                except Exception as exc:
                    why = f"停止自动化失败：{type(exc).__name__}: {exc}"
                    cleanup_errors.append(why)
                    write_event(-1, {"what": "RunnerError", "why": why})
                    abandon_failed = True
                    auto_reposition_pending = False
                else:
                    scene, frame = _battle_scene_state(attempts=3, delay=0.5)
                    hud = is_battle_hud(frame) if frame is not None else None
                    write_event(2, {"what": "BattleStateProbe",
                                    "reason": "timeout" if timed_out else "error",
                                    "scene": scene, "hud": hud,
                                    "actions": action_seen[0]})
                    # 生命点消失不等于退出：暂停/选中干员压暗会让 hud 变 False。
                    # 有画面证据、CopilotAction 或漏怪任一成立就必须走放弃。
                    battle_proven = battle_evidence(
                        scene, actions=action_seen[0], leaked=False)

            def _leak_abort() -> None:
                """漏怪中断：先落 2×2 四宫格，再驱动原生放弃链。"""
                nonlocal abandoned, abandon_failed, abandon_reason
                nonlocal exit_confirmed, auto_reposition_pending, result_page
                abandon_reason = "leak"
                # 证据先行：放弃链可能失败/超时，四宫格必须在动手前就落盘。
                # 该 helper 自吞证据异常，不让拼图/sidecar 失败挡住放弃。
                _capture_leak_evidence()
                verdict = _abandon_and_confirm("leak")
                if verdict == "abandoned":
                    abandoned = True
                    exit_confirmed = True
                elif verdict == "result_page":
                    # 战斗已经失败结束：不再放弃，按结算页归位。
                    result_page = "failed"
                    exit_confirmed = True
                elif verdict == "ended":
                    # 结算标题只证明战斗已结束：不置 result_page，不宣称放弃。
                    # 漏怪证据与 aborted_on_leak 语义由调用方保留。
                    exit_confirmed = True
                else:
                    abandon_failed = True
                    auto_reposition_pending = False

            needs_abandon = (battle_proven and mode != "formation"
                             and ((leak is not None and not allow_leak)
                                  or timed_out or runtime_error is not None))
            if needs_abandon and leak is not None and not allow_leak:
                _leak_abort()
            elif needs_abandon:
                abandon_reason = "timeout" if timed_out else "error"
                verdict = _abandon_and_confirm(abandon_reason)
                if verdict == "abandoned":
                    abandoned = True
                    exit_confirmed = True
                elif verdict == "result_page":
                    result_page = "failed"
                    exit_confirmed = True
                elif verdict == "ended":
                    # 结算标题只证明战斗已结束：battle 保持 unknown。
                    exit_confirmed = True
                else:
                    abandon_failed = True
                    auto_reposition_pending = False
            elif timed_out or runtime_error is not None:
                # 没开战或 HUD 证明已离开战斗：绝不普通导航。
                auto_reposition_pending = False

            if (mode != "formation" and copilot_started and not timed_out
                    and runtime_error is None and not battle_marks
                    and not (leak is not None and not allow_leak)):
                if action_seen[0] == 0:
                    write_event(-1, {"what": "RunnerError",
                                     "why": "战斗未开始（0 个 CopilotAction），跳过结果探测"})
                else:
                    for probe_task in ("ResultFailedCheck", "ResultWinCheck"):
                        if battle_marks:
                            break
                        _run_custom_probe(probe_task, 25)

            # Copilot 链结束后采样仍在继续：这段时间确认的扣血同样要中断。
            # 证据与结算标记互不依赖：MarkBattleWon/失败标记只说明战斗已结束，
            # 不能成为跳过四宫格的理由；只有退出链才需要回避已结算的战斗。
            if mode != "formation" and leak is not None and not allow_leak:
                if (not battle_marks and not abandoned and not abandon_failed
                        and not exit_confirmed):
                    _leak_abort()
                elif leak_evidence_png is None:
                    _capture_leak_evidence()
            if mode == "formation":
                # formation 链完成即完成，不需要战斗退出确认或战后导航。
                auto_reposition_pending = False
            elif (auto_reposition_pending and not exit_confirmed and not abandon_failed
                    and not timed_out and runtime_error is None):
                if battle_marks:
                    # 胜利标记必须有三星奖章佐证，否则划掉并降级为 ended；
                    # 失败标记优先，两者同时存在时不再复核胜利。
                    if ("MarkBattleWon" in battle_marks
                            and "MarkBattleFailed" not in battle_marks):
                        result_page = _won_verdict()
                    exit_confirmed = True
                else:
                    scene, frame = _battle_scene_state(attempts=3, delay=0.5)
                    hud = is_battle_hud(frame) if frame is not None else None
                    write_event(2, {"what": "BattleStateProbe",
                                    "reason": "exit_unconfirmed", "scene": scene,
                                    "hud": hud, "actions": action_seen[0]})
                    panel_ok = False
                    result_page = None

                    def _panel_probe() -> bool:
                        try:
                            return _panel_confirmed()
                        except Exception as exc:
                            write_event(-1, {"what": "RunnerError",
                                             "why": f"原生面板确认失败：{type(exc).__name__}: {exc}"})
                            return False

                    def _result_probe() -> str | None:
                        # 再问一次原生 OCR：任务失败页 / 作战成功页。
                        try:
                            return _result_page_probe()
                        except Exception as exc:
                            write_event(-1, {"what": "RunnerError",
                                             "why": ("原生结算页探测失败："
                                                     f"{type(exc).__name__}: {exc}")})
                            return None

                    def _story_probe() -> dict:
                        """战后剧情页：原生跳过 + 确认，再交给调用方复检画面证据。"""
                        nonlocal asst, copilot_stopped
                        # 剧情链要重新加载基础资源并连接：先停采样、释放当前实例，
                        # 否则两个原生实例会同时操作同一台设备。
                        stop_sampling.set()
                        if sampler is not None and sampler.ident is not None:
                            sampler.join()
                        if asst is not None:
                            with native_lock:
                                running = asst.running()
                                if running and not asst.stop():
                                    raise RuntimeError("剧情推进前停止 MAA 实例失败")
                            asst = None
                            copilot_stopped = True
                        try:
                            story = _advance_post_battle_story(
                                maa_dir=maa_dir, adb=adb, serial=serial,
                                maa_user=str(maa_user))
                        finally:
                            # 复检结算页/详情面板仍要用原生连接：重建同一执行实例。
                            asst = prepare_instance()
                            copilot_stopped = False
                        write_event(2, {"what": "PostBattleStory",
                                        "advanced": story.get("advanced"),
                                        "reason": story.get("reason"),
                                        "before": story.get("before"),
                                        "after": story.get("after"),
                                        "attempts": story.get("attempts"),
                                        "clicked": story.get("clicked"),
                                        "ocr": story.get("ocr"),
                                        "end_of_plot": story.get("end_of_plot"),
                                        "events_path": story.get("events_path")})
                        return story

                    # 右上控制块消失只说明「不是活战斗」，不是结算证据。
                    try:
                        evidence = resolve_exit_evidence(
                            scene, panel_probe=_panel_probe,
                            result_probe=_result_probe, story_probe=_story_probe)
                    except Exception as exc:
                        write_event(-1, {"what": "RunnerError",
                                         "why": ("退出证据复检失败："
                                                 f"{type(exc).__name__}: {exc}")})
                        evidence = {"panel": False, "result_page": None, "story": None}
                    panel_ok = evidence["panel"]
                    result_page = evidence["result_page"]
                    if evidence["story"] is not None and not evidence["story"]["advanced"]:
                        # 剧情推进没有画面证据：保存最后画面，绝不宣称退出。
                        _, last_frame = _battle_scene_state(attempts=1)
                        if last_frame is not None:
                            story_shot = output_dir / "story-unconfirmed-last.png"
                            last_frame.save(story_shot, format="PNG")
                            last_screenshot = str(story_shot)
                        write_event(-1, {"what": "RunnerError",
                                         "why": ("退出未确认：战后剧情推进没有画面证据"
                                                 f"（{evidence['story'].get('reason')}），"
                                                 "已保存最后画面待复核")})
                    if leak is not None and not allow_leak:
                        # 退出探测期间确认了扣血：漏怪优先，先留证据再中断。
                        _leak_abort()
                        verdict = "leak"
                    else:
                        verdict = exit_decision(scene, panel_confirmed=panel_ok,
                                                result_page=result_page)
                        if verdict == "unknown" and story_exit_confirmed(
                                scene, panel_confirmed=panel_ok,
                                result_page=result_page, story=evidence["story"]):
                            verdict = "story"
                    write_event(2, {"what": "ExitDecision", "scene": scene,
                                    "panel": panel_ok, "result_page": result_page,
                                    "verdict": verdict})
                    # verdict == "leak" 已在 _leak_abort 里落定，这里不再处理。
                    if verdict in {"panel", "result_page"}:
                        # 结算页/详情面板都说明战斗已结束：直接归位，不点暂停。
                        exit_confirmed = True
                    elif verdict == "story":
                        # 战后剧情页已被画面探测证明离开：战斗结束成立、继续归位；
                        # 胜负仍以原生结算证据为准，battle 保持 unknown。
                        exit_confirmed = True
                        write_event(2, {"what": "PostBattleStoryExit",
                                        "before": evidence["story"]["before"],
                                        "after": evidence["story"]["after"]})
                    elif verdict == "abandon":
                        abandon_verdict = _abandon_and_confirm("unconfirmed")
                        if abandon_verdict == "abandoned":
                            abandoned = True
                            abandon_reason = "unconfirmed"
                            exit_confirmed = True
                        elif abandon_verdict == "result_page":
                            result_page = "failed"
                            exit_confirmed = True
                        elif abandon_verdict == "ended":
                            # 结算标题只证明战斗已结束：不宣称放弃，battle 保持 unknown。
                            exit_confirmed = True
                        else:
                            abandon_failed = True
                            auto_reposition_pending = False
                    elif verdict == "unknown":
                        # 未知：不宣称退出，也不在非战斗画面上盲点暂停。
                        exit_unconfirmed = True
                        auto_reposition_pending = False
                        write_event(-1, {"what": "RunnerError",
                                         "why": ("退出未确认：画面非战斗但没有原生结算证据，"
                                                 "已保存最后画面待复核")})
    except Exception as exc:
        if guard_pending:
            raise RuntimeError(f"MAA 理智安全检查失败：{exc}") from exc
        runtime_error = f"{type(exc).__name__}: {exc}"
        auto_reposition_pending = False
        write_event(-1, {"what": "RunnerError", "why": runtime_error})
    finally:
        stop_sampling.set()
        # 先等待采样退出，再调用 stop；整个生命周期的原生实例调用均串行化。
        try:
            if sampler is not None and sampler.ident is not None:
                sampler.join()
        finally:
            try:
                if asst is not None:
                    try:
                        with native_lock:
                            if not copilot_stopped and asst.running():
                                if not asst.stop():
                                    raise RuntimeError("MAA stop 返回失败")
                                copilot_stopped = True
                    except Exception as exc:
                        cleanup_errors.append(f"MAA 停止异常：{type(exc).__name__}: {exc}")
                # 放弃失败、退出未确认，或超时/异常后始终没拿到退出证据：都要
                # 保留最后截图，不许把 unknown 当成功。名字区分三种情形。
                exit_evidence_missing = (copilot_started and not exit_confirmed
                                         and (timed_out or runtime_error is not None
                                              or exit_unconfirmed))
                if asst is not None and (abandon_failed or exit_evidence_missing):
                    shot_name = ("abandon-failed-last.png" if abandon_failed
                                 else "exit-unknown-last.png" if exit_unconfirmed
                                 else "battle-exit-last.png")
                    try:
                        with native_lock:
                            image = asst.get_image(resolution[0] * resolution[1] * 3)
                        last_frame = _decode_frame(image, *resolution)
                        if last_frame is None:
                            raise RuntimeError("放弃失败后截图为空")
                        last_screenshot = str(output_dir / shot_name)
                        last_frame.save(last_screenshot, format="PNG")
                    except Exception as exc:
                        cleanup_errors.append(f"放弃失败后截图异常：{type(exc).__name__}: {exc}")
                # 收尾补证：放弃失败、扣血在 abandon_failed 之后才确认、或主流程
                # 异常跳过了中断分支时，四宫格可能还没生成；报告阶段仍需要它。
                # 这里只补证据，不再驱动放弃（退出确认已结束）。结算标记只说明
                # 战斗结束，不能成为跳过漏怪证据的理由；拼图本身只尝试一次。
                if (leak is not None and not allow_leak
                        and leak_evidence_png is None):
                    _capture_leak_evidence()
                if video_proc is not None:
                    try:
                        subprocess.run([adb, "-s", serial, "shell", "pkill", "-INT", "screenrecord"],
                                       timeout=15, capture_output=True)
                        time.sleep(1.5)
                        if video_remote:
                            video_path = output_dir / VIDEO_NAME
                            pull = subprocess.run([adb, "-s", serial, "pull", video_remote, str(video_path)],
                                                  timeout=90, capture_output=True)
                            if (pull.returncode == 0 and video_path.is_file()
                                    and video_path.stat().st_size > 0):
                                video_pulled = True
                                write_event(2, {"what": "VideoSaved", "path": str(video_path)})
                            else:
                                cleanup_errors.append(
                                    "录屏拉取失败：" + pull.stderr.decode("utf-8", "ignore")[:160])
                    except Exception as exc:
                        cleanup_errors.append(f"录屏收尾异常：{type(exc).__name__}: {exc}")
                # stop 的同步回调已结束后再关闭事件文件；异常均进入结构化战报。
                # 采样/回调异常是运行期硬错误，仍写 RunnerError；收尾清理失败
                # （录屏拉取、清单/sidecar 写入）只丢证据，写 RunnerWarning，
                # 不得让 parse_maa_events 把已复核的胜利判成 failed。
                for error in sampling_errors + callback_errors:
                    write_event(-1, {"what": "RunnerError", "why": error})
                for warning in cleanup_errors:
                    write_event(*cleanup_warning_event(warning))
            finally:
                try:
                    with events_lock:
                        events.close()
                finally:
                    # 正常结束/失败/超时都落清单；录像不可用时只写 warnings，不伪造路径。
                    try:
                        _write_review_manifest(
                            manifest_path,
                            _build_review_manifest(
                                timeline, video_started=video_started,
                                video_available=video_pulled,
                                warnings=cleanup_errors,
                            ),
                        )
                    except Exception as exc:
                        cleanup_errors.append(
                            f"复核清单写入失败：{type(exc).__name__}: {exc}")
                    # 释放连接；归位链会重新加载基础资源，不能与旧实例并存。
                    asst = None

    parsed = parse_maa_events(events_path)
    if auto_reposition_pending and exit_confirmed:
        # 只有确认离开战斗后才归位一次，保证连续二次调用无需人工介入。
        # 归位是附加动作：失败必须写进事件与结构化战报，绝不能让异常吞掉
        # result/maa-run.json（事件文件此时已关闭，补写走 append 辅助函数）。
        try:
            _ensure_stage_panel(stage_code, maa_dir=maa_dir, adb=adb,
                                serial=serial, maa_user=str(maa_user),
                                events_path=events_path)
        except Exception as exc:
            reposition_error = f"{type(exc).__name__}: {exc}"
            # 事件文件已关闭（或不可写）时，补写失败只能进 cleanup_errors：
            # 绝不能让异常在战报组装前抛出，吞掉 result/maa-run.json。
            try:
                append_runner_event(events_path, -1,
                                    {"what": "RepositionError",
                                     "why": reposition_error})
            except Exception as log_exc:
                cleanup_errors.append(
                    f"归位失败事件补写异常：{type(log_exc).__name__}: {log_exc}")

    aborted_on_leak = leak is not None and not allow_leak
    # 先取原生胜负，再做任何记账覆盖：MarkBattleWon 只有复核过三星才会产出
    # won；保留但未复核的标记（抓帧失败/画面未知）一律降为 unknown，交给
    # 人工按录像复核。result_page=ended/unknown 只证明战斗结束，不算胜负。
    native_battle = verified_native_battle(
        battle_result_verdict(battle_marks, result_page), win_evidence)
    battle = select_battle_result(
        mode=mode, formation_no_battle=formation_no_battle,
        native_battle=native_battle, abandoned=abandoned,
        abandon_reason=abandon_reason, aborted_on_leak=aborted_on_leak,
        abandon_failed=abandon_failed)
    status = select_run_status(
        formation_no_battle=formation_no_battle, battle=battle,
        abandon_failed=abandon_failed, sanity_blocked=sanity_guard == "blocked",
        timed_out=timed_out, runtime_error=runtime_error,
        exit_unconfirmed=exit_unconfirmed, reposition_error=reposition_error,
        program_status=parsed["program_status"])
    ok = (status == "completed" and not abandon_failed and runtime_error is None
          if mode == "formation" else status == "completed" and battle == "won")
    if last_screenshot is None and frames and (abandon_failed or exit_unconfirmed):
        frozen_name = ("abandon-failed-frozen-last.png" if abandon_failed
                       else "exit-unknown-frozen-last.png")
        last_screenshot = str(output_dir / frozen_name)
        frames[-1].save(last_screenshot, format="PNG")
    report = None
    needs_report = (sanity_guard != "blocked" and not ok
                    and (status in {"failed", "timeout", "ABANDON_FAILED"}
                         or (mode != "formation" and battle != "won")))
    if needs_report:
        if aborted_on_leak and leak_evidence_png is not None:
            # 冻结时已生成 2×2 四宫格，直接复用，避免同一批帧再拼一次。
            report = {
                "reason": "leak",
                "last_action_index": parsed["last_action_index"],
                "last_action": parsed["last_action"],
                "evidence_png": leak_evidence_png,
                "evidence_frames": leak_evidence_frames,
            }
        else:
            report = build_failure_report(
                events_path=events_path, outdir=output_dir, frames=list(frames),
                stage_code=stage_code, tile_grid=tile_grid,
                force=mode != "formation" and battle != "won",
                reason=("leak" if aborted_on_leak else
                        f"battle_{battle}" if mode != "formation" and battle != "won" else None),
            )
    failure = None
    if not ok:
        failure = {
            "reason": failure_reason(
                abandon_failed=abandon_failed, aborted_on_leak=aborted_on_leak,
                sanity_blocked=sanity_guard == "blocked",
                reposition_error=reposition_error,
                report_reason=report["reason"] if report else None),
            "abandon_failed": abandon_failed,
            "exit_confirmed": exit_confirmed,
            "reposition_error": reposition_error,
            "last_action_index": parsed["last_action_index"],
            "last_action": parsed["last_action"],
        }
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["status"] = status
            manifest["battle_result"] = battle
            manifest["ok"] = ok
            _write_review_manifest(manifest_path, manifest)
        except Exception as exc:
            cleanup_errors.append(f"复核清单补写状态失败：{type(exc).__name__}: {exc}")
    result = {
        "ok": ok,
        "battle_result": battle,
        "mode": mode,
        "execution": ("formation" if mode == "formation"
                      else "zero_sanity_official" if zero_sanity else mode),
        "formation": "system" if fixed_squad else "custom",
        "allow_official": allow_official,
        "sanity_guard": sanity_guard,
        "program_status": status,
        "wall_s": round(time.monotonic() - t0, 2),
        "events_path": str(events_path),
        "report_path": str(report_path),
        "review_manifest": str(manifest_path) if manifest_path.is_file() else None,
        "evidence_png": report["evidence_png"] if report else None,
        "failure": failure,
        "leak": leak,
        "allow_leak": allow_leak,
        "last_screenshot": last_screenshot,
        "action_count": parsed["action_count"],
        "sampling_error": "; ".join(sampling_errors) or None,
        "note": ("MAA chain completed 只表示程序完成，不等于战斗胜利；"
                 "formation 成功只代表编队链完成"),
    }
    summary = {
        **result,
        "task": "Copilot", "params": params, "task_id": task_id,
        "chains_seen": sorted(chains), "stage": stage_code, "overlay": str(overlay),
        "events": str(events_path), "maa_user": str(maa_user), "report": report,
        "runtime_error": runtime_error, "cleanup_errors": cleanup_errors,
        # 同一批收尾失败换个显式名字：这些只是证据/清单落盘警告，不影响
        # program_status 与 ok；cleanup_errors 保留原字段供既有复核脚本读取。
        "cleanup_warnings": list(cleanup_errors),
        "formation_no_battle": formation_no_battle, "exit_confirmed": exit_confirmed,
        "native_battle": native_battle, "reposition_error": reposition_error,
        "win_evidence": win_evidence, "leak_evidence_error": leak_evidence_error,
    }
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MAA Copilot 执行器（默认仅演习）")
    ap.add_argument("--stage", default="unknown", help="战报中的关卡代号")
    ap.add_argument("--outdir", required=True, help="战报输出目录")
    ap.add_argument("--squad", required=True, help="Copilot 作业 JSON")
    ap.add_argument("--mode", choices=sorted(MODE_OVERLAY), default="exercise")
    ap.add_argument("--maa-dir", default=DEFAULT_MAA, help="MAA 安装目录")
    ap.add_argument("--overlay", default=None, help="覆盖该模式的 overlay 目录")
    ap.add_argument("--adb", default=DEFAULT_ADB, help="由 MAA 原生连接使用的 adb 路径")
    ap.add_argument("--serial", default=DEFAULT_SERIAL, help="模拟器设备地址")
    ap.add_argument("--formation-index", type=int, default=4, help="编队编号")
    ap.add_argument("--maa-user", default=str(ROOT / "data" / "maa_userdata"),
                    help="MAA 持久化用户目录，用于复用原生头像缓存")
    ap.add_argument("--timeout", type=float, default=600, help="执行超时秒数")
    ap.add_argument("--support", action="store_true", help="允许使用一名助战干员")
    ap.add_argument("--official", action="store_true", help="兼容旧参数，等同 --mode official")
    args = ap.parse_args(argv)
    result = run_copilot(
        args.stage, args.squad, mode="official" if args.official else args.mode,
        outdir=args.outdir, timeout=args.timeout, support=args.support,
        maa_dir=args.maa_dir, adb=args.adb, serial=args.serial, maa_user=args.maa_user,
        overlay=args.overlay, formation_index=args.formation_index,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Unified CLI for the Arknights automation engine.

  python cli.py intel    <stage_code> [--refresh] [--refs N]
  python cli.py validate <stage_code> <squad_json> [--intel PATH]
  python cli.py run      <stage_code> <squad_json> [--mode exercise|official|formation]
                         [--support] [--outdir DIR] [--maa-user DIR]
  python cli.py record   <stage_code> --outdir DIR
  python cli.py doc      [query] [--section KEY] [--refresh]

Stage codes are resolved at runtime (MAA stages.json + unpacked stage_table);
runtime data is stored under data/ and is not distributed with the source.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def _default_outdir(stage: str, mode: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return str(ROOT / "data" / "captures" / f"{stage}-{mode}-{stamp}")


def cmd_intel(args) -> int:
    from src.planner.intel import main as intel_main
    argv = [args.stage, "--refs", str(args.refs)]
    if args.refresh:
        argv.append("--refresh")
    return intel_main(argv)


def cmd_validate(args) -> int:
    from src.engine.validator import main as validate_main
    argv = [args.stage, args.squad]
    if args.intel:
        argv += ["--intel", args.intel]
    return validate_main(argv)


def cmd_run(args) -> int:
    from src.engine.runner import main as run_main
    outdir = args.outdir or _default_outdir(args.stage, args.mode)
    argv = [
        "--stage", args.stage,
        "--squad", args.squad,
        "--outdir", outdir,
        "--mode", args.mode,
        "--formation-index", str(args.formation_index),
        "--timeout", str(args.timeout),
    ]
    if args.maa_user:
        argv += ["--maa-user", args.maa_user]
    if args.support:
        argv.append("--support")
    if args.official:
        argv.append("--official")
    print(f"[cli] run mode={args.mode} outdir={outdir}", file=sys.stderr)
    return run_main(argv)


def cmd_record(args) -> int:
    from src.engine.recorder import main as record_main
    argv = ["--stage", args.stage, "--outdir", args.outdir]
    if args.events:
        argv += ["--events", args.events]
    if args.tile_grid:
        argv += ["--tile-grid", args.tile_grid]
    return record_main(argv)


def cmd_doc(args) -> int:
    from src.planner.doc_fetcher import lookup

    print(lookup(args.query, args.section, args.refresh))
    return 0


def cmd_campaign(args) -> int:
    """通用单关推进：读关卡画像 → 按策略跑演习/正式，并打印结构化结果。"""

    from src.planner.intel import resolve_stage_profile
    from src.engine.runner import run_copilot

    profile = resolve_stage_profile(args.stage)
    if args.mode == "official" and not profile["zero_sanity"] and not args.authorize_official:
        print("拒绝：非零理智关卡的 official 必须显式 --authorize-official", file=sys.stderr)
        return 2
    outdir = args.outdir or _default_outdir(args.stage, args.mode)
    report = run_copilot(
        args.stage, args.squad, mode=args.mode, outdir=outdir, timeout=args.timeout,
    )
    print(json.dumps({"profile": profile, "report": report}, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


def cmd_specs(args) -> int:
    """查询干员精炼面板（费用/阻挡/技能/射程格）。"""
    from src.planner.intel import query_operator_specs

    names = [item.strip() for item in args.names.split(",") if item.strip()] or None
    result = query_operator_specs(names, args.role or "")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if "error" not in result else 1


def cmd_demo(args) -> int:
    """只读监听人类触控，输出反算格子的动作流与示教连环画。"""
    from src.engine.teach_recorder import record_human_demo

    demo = record_human_demo(args.stage, args.duration)
    print(json.dumps(demo, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="cli.py", description="Arknights automation engine")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("doc", help="look up local knowledge, MAA protocols or PRTS terms on demand")
    p.add_argument("query", nargs="?", default="", help="topic or term; omit to list topics")
    p.add_argument("--section", default="", metavar="KEY", help="select a matching document section")
    p.add_argument("--refresh", action="store_true", help="refresh the cached document")
    p.set_defaults(func=cmd_doc)

    p = sub.add_parser("intel", help="fetch stage intel (tiles/waves/boss/roster/references)")
    p.add_argument("stage")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--refs", type=int, default=3)
    p.set_defaults(func=cmd_intel)

    p = sub.add_parser("validate", help="validate a copilot job against stage intel")
    p.add_argument("stage")
    p.add_argument("squad")
    p.add_argument("--intel", default=None)
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("run", help="run one MAA copilot job")
    p.add_argument("stage")
    p.add_argument("squad")
    p.add_argument("--mode", choices=["exercise", "official", "formation"], default="exercise")
    p.add_argument("--support", action="store_true")
    p.add_argument("--official", action="store_true", help="legacy alias for --mode official")
    p.add_argument("--outdir", default=None)
    p.add_argument("--maa-user", default=None)
    p.add_argument("--formation-index", type=int, default=4)
    p.add_argument("--timeout", type=float, default=600)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("record", help="离线解析 MAA 事件，生成战报与失败证据图")
    p.add_argument("stage")
    p.add_argument("--outdir", required=True)
    p.add_argument("--events", default=None, help="maa-events.jsonl 路径，默认取 outdir 内同名文件")
    p.add_argument("--tile-grid", default=None, help="intel JSON 或含 tiles 字段的网格 JSON")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("campaign", help="单关通用推进（读画像 → 演习/正式）")
    p.add_argument("stage")
    p.add_argument("squad")
    p.add_argument("--mode", choices=["exercise", "official", "formation"], default="exercise")
    p.add_argument("--authorize-official", action="store_true")
    p.add_argument("--outdir", default=None)
    p.add_argument("--timeout", type=float, default=420)
    p.set_defaults(func=cmd_campaign)

    p = sub.add_parser("specs", help="查询干员精炼面板（费用/阻挡/技能/射程格）")
    p.add_argument("names", nargs="?", default="", help="逗号分隔的干员名；省略则全量")
    p.add_argument("--role", default="", help="职业过滤，如 狙击 / SNIPER")
    p.set_defaults(func=cmd_specs)

    p = sub.add_parser("demo", help="只读采集人类触控示范并反算格子")
    p.add_argument("stage")
    p.add_argument("--duration", type=float, default=20.0)
    p.set_defaults(func=cmd_demo)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

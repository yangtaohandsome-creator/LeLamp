"""Repeatable OpenClaw/Pi Agent benchmark against the recording Control API."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
import uuid
from pathlib import Path

import httpx


CASES = [
    ("explicit_nod", "点个头", [["play_motion"]]),
    ("asr_nod", "有我点个头了", [["play_motion"]]),
    ("explicit_headshake", "摇个头", [["play_motion"]]),
    ("praise", "你很聪明啊", [["queue_expression"]]),
    ("insult", "你蠢死了", [["queue_expression"]]),
    ("turn_left", "向左转30度", [["turn_base"]]),
    ("absolute_right", "面向最右边", [["set_base_heading"]]),
    ("front", "回到正前方", [["reset_base_heading"], ["set_base_heading"]]),
    ("heading_query", "你现在朝向哪里", [["get_robot_state"]]),
    ("work_light", "帮我照桌面", [["enter_work_light"]]),
    ("work_low_warm", "进入低姿态暖黄光照明", [["enter_work_light"]]),
    ("work_brighter", "办公灯亮一点", [["update_work_light"]]),
    ("exit_work", "退出办公模式", [["exit_work_light"]]),
    ("delayed_nod", "15秒后点个头", [["create_timer"]]),
    ("plain_timer", "定时25分钟提醒我喝水", [["create_timer"]]),
    ("daily_alarm", "每天早上十点半提醒我喝水", [["create_alarm"]]),
    ("list_alarm", "现在有哪些闹钟", [["list_alarms"]]),
    ("false_condition", "先看看现在是否处于办公模式，如果是，十秒后提醒我休息", [["get_robot_state"]]),
    ("negated_sleep", "不要睡眠", [[]]),
    ("quoted_goodbye", "别人说再见是什么意思", [[], ["queue_expression"]]),
    ("goodbye", "拜拜", [["sleep"]]),
    ("identity", "你是一盏台灯吗", [["queue_expression"]]),
]


def arguments_correct(case_id: str, calls: list[dict]) -> bool:
    if not calls:
        return True
    args = calls[0]["arguments"]
    if case_id == "turn_left":
        return args.get("direction") == "left" and args.get("steps", 1) == 1
    if case_id == "absolute_right":
        return args.get("position") == "right"
    if case_id == "work_low_warm":
        return args.get("pose") == "low" and args.get("tone") == "warm"
    if case_id == "work_brighter":
        return args.get("brightness_step") == 25
    if case_id == "delayed_nod":
        action = args.get("on_complete", {})
        return args.get("duration_seconds") == 15 and action.get("tool") == "play_motion" and action.get("arguments", {}).get("name") == "nod"
    if case_id == "plain_timer":
        return args.get("duration_seconds") == 1500 and "on_complete" not in args and "agent_task" not in args
    if case_id == "daily_alarm":
        return args.get("recurrence") == "daily" and "T10:30:00+08:00" in str(args.get("trigger_at", ""))
    return True


def leaked_internal_text(answer: str) -> bool:
    markers = ("According to the rules", "Let me call", "The user says", "NO_REPLY", "lelamp_")
    return any(marker in answer for marker in markers)


async def request(backend: str, text: str, session: str) -> tuple[dict, float]:
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=65) as client:
        if backend == "pi":
            response = await client.post(
                os.getenv("PI_AGENT_URL", "http://127.0.0.1:18792") + "/v1/chat",
                headers={"Authorization": f"Bearer {os.environ['PI_AGENT_TOKEN']}"},
                json={"session_id": session, "text": text, "context": "当前城市上海，时区 Asia/Shanghai，当前时间 2026-09-15T18:00:00+08:00。最终回答直接播报。"},
            )
        else:
            response = await client.post(
                os.getenv("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789") + "/v1/chat/completions",
                headers={"Authorization": f"Bearer {os.environ['OPENCLAW_GATEWAY_TOKEN']}", "x-openclaw-agent-id": "lelamp"},
                json={
                    "model": "openclaw/lelamp", "stream": False, "max_tokens": 512,
                    "user": f"lelamp-{session}",
                    "messages": [
                        {"role": "system", "content": "当前城市上海，时区 Asia/Shanghai，当前时间 2026-09-15T18:00:00+08:00。最终回答直接播报。"},
                        {"role": "user", "content": text},
                    ],
                },
            )
        response.raise_for_status()
        return response.json(), (time.perf_counter() - started) * 1000


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("pi", "openclaw"), required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--cases", default="", help="comma-separated case ids")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    selected = set(filter(None, args.cases.split(",")))
    cases = [item for item in CASES if not selected or item[0] in selected]
    records = []
    async with httpx.AsyncClient() as admin:
        for repeat in range(args.repeats):
            for case_id, text, allowed in cases:
                await admin.post("http://127.0.0.1:18793/reset")
                session = f"bench-{args.backend}-{case_id}-{repeat}-{uuid.uuid4().hex[:8]}"
                error = None
                payload = {}
                elapsed = 0.0
                try:
                    payload, elapsed = await request(args.backend, text, session)
                except Exception as exc:
                    error = str(exc)
                observed = (await admin.get("http://127.0.0.1:18793/calls")).json()["calls"]
                names = [item["name"] for item in observed]
                args_ok = arguments_correct(case_id, observed)
                tool_wait = None
                if observed:
                    tool_wait = max(0.0, time.time() - observed[-1]["finished_at"]) * 1000
                answer = payload.get("answer", "") if args.backend == "pi" else (
                    payload.get("choices", [{}])[0].get("message", {}).get("content", "")
                )
                leaked = leaked_internal_text(answer)
                correct = names in allowed and args_ok and not leaked
                item = {"backend": args.backend, "case": case_id, "repeat": repeat, "text": text, "tools": observed, "allowed": allowed, "arguments_correct": args_ok, "analysis_leak": leaked, "correct": correct, "answer": answer, "total_ms": elapsed, "tool_to_response_ms": tool_wait, "error": error, "backend_metrics": payload.get("metrics")}
                records.append(item)
                print(f"{args.backend} {case_id} #{repeat + 1}: {'PASS' if correct and not error else 'FAIL'} {elapsed / 1000:.2f}s {names}", flush=True)
    totals = [item["total_ms"] for item in records if not item["error"]]
    waits = [item["tool_to_response_ms"] for item in records if item["tool_to_response_ms"] is not None]
    summary = {
        "backend": args.backend, "cases": len(records),
        "correct": sum(bool(item["correct"] and not item["error"]) for item in records),
        "errors": sum(bool(item["error"]) for item in records),
        "p50_ms": statistics.median(totals) if totals else None,
        "p95_ms": sorted(totals)[max(0, int(len(totals) * .95) - 1)] if totals else None,
        "tool_wait_p50_ms": statistics.median(waits) if waits else None,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"summary": summary, "records": records}, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())

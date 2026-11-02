#!/usr/bin/env python3
"""定期実行（SYNC_HEADLESS=1）中の Slack 書き込みを制限する PreToolUse hook。

- 送信は本人宛て DM だけに限る
- キャンバスの更新は ToDo のキャンバス（state/todo-canvas.json）だけに限る。作成はさせない
- リストへの書き込みは ToDo のリスト（state/todo-list.json）だけに限る。列の追加・削除・改名はさせない（選択肢の追加だけ許す）
- SYNC_DRY_RUN=1 のときは送信も下書き作成もすべて止める
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TODO_CANVAS = ROOT / "state" / "todo-canvas.json"
TODO_LIST = ROOT / "state" / "todo-list.json"

if os.environ.get("SYNC_HEADLESS") != "1":
    sys.exit(0)

SELF_USER_ID = json.loads((ROOT / "config.json").read_text())["slack_user_id"]
payload = json.load(sys.stdin)
tool = payload.get("tool_name", "")
tool_input = payload.get("tool_input") or {}

if os.environ.get("SYNC_DRY_RUN") == "1":
    print("dry run 中は Slack への送信・下書き作成・キャンバスやリストの更新はできません。本文は state/outbox-<skill名>.md に書いてください", file=sys.stderr)
    sys.exit(2)
if tool.endswith("slack_create_canvas"):
    print("定期実行中はキャンバスを作成できません", file=sys.stderr)
    sys.exit(2)
if tool.endswith("slack_update_canvas"):
    allowed = json.loads(TODO_CANVAS.read_text())["canvas_id"]
    if tool_input.get("canvas_id") != allowed:
        print(f"定期実行中に更新できるのは ToDo のキャンバス ({allowed}) だけです", file=sys.stderr)
        sys.exit(2)
if tool.endswith("slack_send_message") and tool_input.get("channel_id") != SELF_USER_ID:
    print(f"定期実行中は本人宛て DM ({SELF_USER_ID}) 以外へは送信できません", file=sys.stderr)
    sys.exit(2)
if tool.endswith("slack_create_list"):
    print("定期実行中はリストを作成できません", file=sys.stderr)
    sys.exit(2)
if tool.endswith(("slack_add_list_record", "slack_update_list_record", "slack_update_list")):
    allowed = json.loads(TODO_LIST.read_text())["list_id"]
    if tool_input.get("list_id") != allowed:
        print(f"定期実行中に書き込めるのは ToDo のリスト ({allowed}) だけです", file=sys.stderr)
        sys.exit(2)
    if tool.endswith("slack_update_list") and (tool_input.get("columns_to_add") or tool_input.get("columns_to_delete")
                                               or any(set(c) - {"key", "options"} for c in tool_input.get("columns_to_update") or [])
                                               or any(k in tool_input for k in ("name", "description", "icon"))):
        print("定期実行中にできるリストの設定変更は、選択肢の追加だけです", file=sys.stderr)
        sys.exit(2)
sys.exit(0)

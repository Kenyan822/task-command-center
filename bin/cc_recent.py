#!/usr/bin/env python3
"""各プロジェクトの Claude Code セッション履歴から、指定時刻以降に動いたセッションの要点を抜き出す。

transcript は 1 本数 MB あるため LLM に丸ごと渡さず、ユーザー指示と最後の応答だけを出す。
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"
ROOT = Path(__file__).resolve().parent.parent
_config = json.loads((ROOT / "config.json").read_text())
# Claude Code は作業ディレクトリの絶対パスの記号を "-" に置き換えた名前で履歴を保存する
ROOT_PREFIX = re.sub(r"[^A-Za-z0-9]", "-", str(Path(_config["cc_projects_root"]).expanduser())) + "-"
SELF_DIR = re.sub(r"[^A-Za-z0-9]", "-", str(ROOT))
# bot の実行ごとの作業ディレクトリなど、人が操作していないセッションは除外する
EXCLUDE_MARKERS = ("-data-runs-",)


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def is_human_prompt(text):
    t = text.lstrip()
    return bool(t) and not t.startswith(("<local-command", "<command-", "<system-reminder", "<task-notification", "Caveat:"))


def summarize(path, since):
    title, branch, cwd, last_ts, first_ts = None, None, None, None, None
    prompts, replies = [], []
    for line in path.open(encoding="utf-8", errors="replace"):
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = o.get("type")
        if t == "ai-title":
            title = o.get("aiTitle")
            continue
        if t not in ("user", "assistant") or o.get("isSidechain"):
            continue
        ts = o.get("timestamp")
        if not ts:
            continue
        cwd = o.get("cwd") or cwd
        branch = o.get("gitBranch") or branch
        last_ts = ts
        if ts < since:
            continue
        first_ts = first_ts or ts
        txt = text_of((o.get("message") or {}).get("content"))
        if t == "user" and not o.get("isMeta") and is_human_prompt(txt):
            prompts.append((ts, txt.strip()[:400]))
        elif t == "assistant" and txt.strip():
            replies.append((ts, txt.strip()[:800]))
    if not last_ts or last_ts < since:
        return None
    project = path.parent.name[len(ROOT_PREFIX):]
    # 日本語のディレクトリ名はエンコード時に "-" に潰れるので cwd から復元する
    if not project.strip("-") and cwd:
        project = Path(cwd).name
    return {
        "project": project,
        "project_cwd": cwd,
        "session_id": path.stem,
        "title": title,
        "branch": branch,
        "first_activity_since": first_ts,
        "last_activity": last_ts,
        "prompts_since": prompts[-8:],
        "last_replies": replies[-2:],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True, help="ISO8601 UTC 例: 2026-10-09T00:00:00Z")
    args = ap.parse_args()
    since = args.since
    datetime.fromisoformat(since.replace("Z", "+00:00")).astimezone(timezone.utc)

    out = []
    for d in sorted(PROJECTS_DIR.iterdir()):
        if not d.name.startswith(ROOT_PREFIX) or any(m in d.name for m in EXCLUDE_MARKERS):
            continue
        if d.name == SELF_DIR:
            continue
        for f in d.glob("*.jsonl"):
            mtime = datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if mtime < since:
                continue
            s = summarize(f, since)
            if s:
                out.append(s)
    out.sort(key=lambda s: s["last_activity"], reverse=True)
    json.dump(out, sys.stdout, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()

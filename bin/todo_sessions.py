#!/usr/bin/env python3
"""ToDo の当日のコードのタスクごとに、Orca の worktree と Claude のセッションを用意する。

対象は、予定日が今日まで（直近 7 日）で未完了、issue の列が Orca に登録したリポジトリの issue・PR を指す行。
- serve：localhost で待ち受け、/open?todo=<Record ID> で該当の Orca の端末を前に出す（無ければ作る・閉じていれば claude --resume で再開する）。
  あわせて 10 分ごとに prepare と cleanup を回す（launchd から常駐）
- prepare：対象の行に worktree を作り、Claude に環境構築だけさせ、終わったら端末を閉じてメモリを空ける。1 件ずつ順に進める
- cleanup：済みにした・消した行の端末を閉じる。worktree は消さない（消すのは本人の確認後）
- targets：対象の行と、リストの「セッション」の列に書く URL を出す（同期が使う）
- stale：端末を閉じた worktree（片付け候補）を出す

状態は state/todo-sessions.json。Docker は立ち上げない（環境構築では依存の導入と設定ファイルの用意まで）。
無人で起動する Claude は確認を飛ばさず、環境構築に要る操作だけを許す（それ以外は確認待ちで止まり、本人が開いたときに答える）。
"""
import argparse
import json
import re
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from todo_list import CSV_PATH, DONE, JST, STATE, load_rows, today  # noqa: E402

SESSIONS = STATE / "todo-sessions.json"
PORT = 47615
LOOKBACK_DAYS = 7
MIN_FREE_BYTES = 2 * 1024 ** 3
SETUP_TIMEOUT_MS = 40 * 60 * 1000
SETUP_STALL_S = 10 * 60
WORKER_INTERVAL_S = 600
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
REPO_URL = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/(issues|pull)/(\d+)")

SETUP_TOOLS = [
    "Read", "Glob", "Grep",
    "Bash(ls:*)", "Bash(git status:*)", "Bash(git log:*)", "Bash(git branch --show-current:*)",
    "Bash(gh issue view:*)", "Bash(gh pr view:*)",
    "Bash(npm install:*)", "Bash(npm ci:*)", "Bash(pnpm install:*)", "Bash(yarn install:*)",
    "Bash(uv sync:*)", "Bash(npx prisma generate:*)",
    "Bash(npx tsc --noEmit:*)", "Bash(npm run typecheck:*)", "Bash(npm run build:*)", "Bash(npm run lint:*)",
    "Bash(pnpm typecheck:*)", "Bash(pnpm build:*)", "Bash(pnpm lint:*)",
]

SETUP_PROMPT = """ToDo「{name}」の作業環境を用意してください。今は環境構築だけで、実装はまだしません。

- 対象：{issue}（`gh issue view` / `gh pr view` で読めます）
- 元の checkout：{origin}

やること
1. リポジトリの README・CLAUDE.md・package.json などから、開発に要る準備を確かめる
2. 依存を入れる（npm / pnpm / uv など、リポジトリの慣習どおり）
3. git 管理外の設定ファイル（.env など）が要るなら、元の checkout から cp で写す。中身は表示しない
4. 型チェックやビルドなど、すぐ終わる確認が通るか見る。通らなければ原因をメモして直さない
5. issue を読み、着手の方針を 3〜5 行でまとめて最後に書く

守ること
- Docker・DB・開発サーバーは起動しない（メモリを使うため。作業に入るときに起動する）
- コードの変更・コミット・プッシュはしない
- ブランチ名は Orca が付けた仮のもの。作業に入るときにリポジトリの慣習に合わせて付け直す
- 許可されていない操作が要るときは、確認を出してそのまま待つ（本人が開いたときに答える）
- 終わったら何もせず待つ（この端末は自動で閉じ、本人が開くときに再開します）
"""

lock = threading.Lock()


def log(msg):
    print(f"{datetime.now(JST):%F %T} {msg}", flush=True)


def orca(*args, check=True):
    p = subprocess.run(["orca", *args, "--json"], capture_output=True, text=True)
    try:
        out = json.loads(p.stdout)
    except json.JSONDecodeError:
        out = {"ok": False, "error": {"message": (p.stdout + p.stderr).strip()}}
    if check and not out.get("ok"):
        raise RuntimeError(f"orca {' '.join(args[:2])}: {out.get('error', {}).get('message', out)}")
    return out.get("result") or {}


def orca_running():
    try:
        orca("status")
        return True
    except RuntimeError:
        return False


def load_sessions():
    if SESSIONS.exists():
        return json.loads(SESSIONS.read_text())
    return {"rows": {}}


def save_sessions(s):
    SESSIONS.write_text(json.dumps(s, ensure_ascii=False, indent=1))


def repo_map():
    """GitHub の owner/name → Orca の repo（id・path）。"""
    out = {}
    for r in orca("repo", "list").get("repos", []):
        url = subprocess.run(["git", "-C", r["path"], "remote", "get-url", "origin"],
                             capture_output=True, text=True).stdout.strip()
        m = re.search(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?$", url)
        if m:
            out[f"{m.group(1)}/{m.group(2)}".lower()] = {"id": r["id"], "path": r["path"]}
    return out


def targets(rows, repos):
    now = today()
    for r in rows:
        if r["status"] == DONE or not r["planned"] or not (now - timedelta(days=LOOKBACK_DAYS) <= r["planned"] <= now):
            continue
        m = REPO_URL.match(r["issue"])
        repo = m and repos.get(f"{m.group(1)}/{m.group(2)}".lower())
        if repo:
            yield r, repo, m


def free_bytes():
    vm = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    page = int(re.search(r"page size of (\d+)", vm).group(1))
    pages = sum(int(re.search(rf"Pages {k}:\s+(\d+)", vm).group(1)) for k in ("free", "inactive", "speculative", "purgeable"))
    return pages * page


def worktree_terminals(worktree_id):
    return [t for t in orca("terminal", "list").get("terminals", []) if t.get("worktreeId") == worktree_id]


def latest_claude_session(path):
    d = CLAUDE_PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", path)
    files = sorted(d.glob("*.jsonl"), key=lambda f: f.stat().st_mtime) if d.exists() else []
    return files[-1].stem if files else None


def setup_command(row, origin):
    tools = SETUP_TOOLS + [f"Bash(cp {origin}/.env:*)"]
    prompt = SETUP_PROMPT.format(name=row["name"], issue=row["issue"], origin=origin)
    # --strict-mcp-config：.mcp.json の承認待ちで止まらないように、また本番に繋がる MCP を無人で起動しないように、MCP を読まない
    return shlex.join(["claude", "--permission-mode", "default", "--strict-mcp-config", "--allowedTools", *tools, "--", prompt])


def create(row, repo, m, focus):
    """worktree を作り、権限を絞った Claude に環境構築をさせる。戻り値は状態と端末の handle。"""
    name = f"todo-{m.group(2)}-{m.group(4)}"
    wt = orca("worktree", "create", "--repo", f"id:{repo['id']}", "--name", name, "--no-parent",
              "--reference", row["issue"]).get("worktree", {})
    # Orca はブランチ名を worktree 名から作る（頭に付くものは Orca の branchPrefix 次第）ので、リポジトリの慣習の feature/<issue 番号> に付け直す
    subprocess.run(["git", "-C", wt["path"], "branch", "-m", f"feature/{m.group(4)}"], check=True)
    args = ["terminal", "create", "--worktree", f"id:{wt['id']}", "--command", setup_command(row, repo["path"]),
            "--title", name]
    if focus:
        args.append("--focus")
    handle = orca(*args).get("terminal", {}).get("handle")
    return {"worktree": wt["id"], "path": wt["path"], "issue": row["issue"], "name": row["name"], "title": name, "handle": handle,
            "phase": "open" if focus else "setup", "created_at": datetime.now(JST).isoformat(timespec="seconds")}, handle


def prepare():
    if not orca_running():
        log("prepare skip: Orca が起動していない")
        return
    rows = load_rows(CSV_PATH)
    repos = repo_map()
    for row, repo, m in targets(rows, repos):
        with lock:
            s = load_sessions()
            if row["id"] in s["rows"]:
                continue
            if free_bytes() < MIN_FREE_BYTES:
                log(f"prepare stop: 空きメモリが {free_bytes() / 1024 ** 3:.1f}GB しかない（残りはクリック時に作る）")
                return
            entry, handle = create(row, repo, m, focus=False)
            s["rows"][row["id"]] = entry
            save_sessions(s)
        log(f"prepare {row['id']} → {entry['path']}")
        result = wait_setup(entry["path"]) if handle else "no-terminal"
        with lock:
            s = load_sessions()
            if s["rows"][row["id"]]["phase"] == "setup":
                orca("terminal", "close", "--worktree", f"id:{entry['worktree']}", "--all", check=False)
                s["rows"][row["id"]]["phase"] = "ready"
                s["rows"][row["id"]]["setup_result"] = result
                save_sessions(s)
                log(f"prepare {row['id']} 端末を閉じた（{result}）")


def wait_setup(path):
    """環境構築の終わりを Claude の記録で見る。端末の idle は道具の呼び出しの合間にも立つので使わない。"""
    deadline = time.time() + SETUP_TIMEOUT_MS / 1000
    while time.time() < deadline:
        time.sleep(15)
        sid = latest_claude_session(path)
        if not sid:
            continue
        f = CLAUDE_PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", path) / f"{sid}.jsonl"
        last = None
        for line in f.read_text().splitlines():
            r = json.loads(line)
            if r.get("type") in ("user", "assistant"):
                last = r
        if last and last["type"] == "assistant" and last["message"].get("stop_reason") == "end_turn":
            return "done"
        if time.time() - f.stat().st_mtime > SETUP_STALL_S:
            return "stalled"  # 許可していない操作の確認待ちで止まっている見込み。開いたときに本人が答える
    return "timeout"


def cleanup():
    if not orca_running():
        return
    rows = {r["id"]: r for r in load_rows(CSV_PATH)}
    with lock:
        s = load_sessions()
        for rid, e in s["rows"].items():
            row = rows.get(rid)
            if e["phase"] != "closed" and (row is None or row["status"] == DONE):
                orca("terminal", "close", "--worktree", f"id:{e['worktree']}", "--all", check=False)
                e["phase"] = "closed"
                e["closed_at"] = datetime.now(JST).isoformat(timespec="seconds")
                log(f"cleanup {rid} 端末を閉じた（{e['path']}）")
        save_sessions(s)


def open_row(rid):
    """該当の端末を前に出す。戻り値は画面に出す一文。"""
    if not orca_running():
        orca("open")
    with lock:
        s = load_sessions()
        e = s["rows"].get(rid)
        if e is None:
            hit = next(((r, repo, m) for r, repo, m in targets(load_rows(CSV_PATH), repo_map()) if r["id"] == rid), None)
            if hit is None:
                return "この行はセッションの対象ではありません（予定日が今日まで・issue が Orca のリポジトリを指す行だけ）"
            e, _ = create(*hit, focus=True)
            s["rows"][rid] = e
            save_sessions(s)
            subprocess.run(["open", "-a", "Orca"])
            return f"worktree を作り、環境構築を始めました（{e['path']}）"
        # Orca が worktree を作るときに開くシェルのタブではなく、Claude を動かしている端末を選ぶ（タイトルは Claude が書き換えるので handle で見る）
        if any(t["handle"] == e.get("handle") for t in worktree_terminals(e["worktree"])):
            orca("terminal", "switch", "--terminal", e["handle"])
            msg = "開いている端末に切り替えました"
        else:
            sid = latest_claude_session(e["path"])
            cmd = shlex.join(["claude", "--resume", sid] if sid else ["claude"])
            e["handle"] = orca("terminal", "create", "--worktree", f"id:{e['worktree']}", "--command", cmd, "--title", e["title"],
                               "--focus").get("terminal", {}).get("handle")
            msg = "セッションを再開しました" if sid else "セッションの記録が無いので新しく起動しました"
        e["phase"] = "open"
        e.pop("closed_at", None)
        save_sessions(s)
    subprocess.run(["open", "-a", "Orca"])
    return f"{msg}（{e['path']}）"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        rid = (parse_qs(u.query).get("todo") or [""])[0]
        if u.path != "/open" or not re.fullmatch(r"Rec\w+", rid):
            self.send_error(404)
            return
        try:
            msg, code = open_row(rid), 200
        except Exception as ex:  # 画面に失敗を出し、ログにも残す
            msg, code = f"失敗しました：{ex}", 500
            log(f"open {rid} failed: {ex}")
        body = f"<!doctype html><meta charset=utf-8><title>Orca</title><p>{msg}</p><script>setTimeout(()=>window.close(),1500)</script>"
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, fmt, *args):
        log(fmt % args)


def worker():
    while True:
        for f in (cleanup, prepare):
            try:
                f()
            except Exception as ex:
                log(f"{f.__name__} failed: {ex}")
        time.sleep(WORKER_INTERVAL_S)


def cmd_serve(args):
    threading.Thread(target=worker, daemon=True).start()
    log(f"serve http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


def cmd_targets(args):
    for row, repo, m in targets(load_rows(CSV_PATH), repo_map()):
        print(f"{row['id']}\thttp://localhost:{PORT}/open?todo={row['id']}\t{'set' if row['session'] else 'empty'}\t{row['name']}")


def cmd_stale(args):
    for rid, e in load_sessions()["rows"].items():
        if e["phase"] == "closed":
            print(f"{rid}\t{e['path']}\t{e.get('closed_at', '')}\t{e['name']}")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(required=True)
    sub.add_parser("serve").set_defaults(func=cmd_serve)
    sub.add_parser("prepare").set_defaults(func=lambda a: prepare())
    sub.add_parser("cleanup").set_defaults(func=lambda a: cleanup())
    sub.add_parser("targets").set_defaults(func=cmd_targets)
    sub.add_parser("stale").set_defaults(func=cmd_stale)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())

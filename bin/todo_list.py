#!/usr/bin/env python3
"""Slack リスト「ToDo ボード」を正として、todo.md・キャンバス・朝の DM の本文を作る。

入力は slack_read_list(format=csv) の出力をそのまま保存した CSV（既定 state/todo-list-current.csv）。
行の出入りと「済み」になった日は state/todo-list-state.json に残す（リストには済みにした日時の列が無いため）。

- sync：状態ファイルを更新して state/todo.md を書き直し、前回からの変化を JSON で出す
- mark-added <record_id> <ledger_id>：同期が足した行を記録する（🆕 と ⚠️ の判定に使う）
- canvas [--if-changed] / digest：キャンバスの本文・朝の DM の本文を出す
- issues：未完了で issue が付いた行を出す（閉じたか gh で確かめるため）
"""
import argparse
import csv
import io
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
CSV_PATH = STATE / "todo-list-current.csv"
CANVAS_BODY = STATE / "todo-canvas-body.md"
LIST_STATE = STATE / "todo-list-state.json"
TODO = STATE / "todo.md"
LEDGER = STATE / "ledger.json"
JST = timezone(timedelta(hours=9))

DONE = "済み"
DAY_LIMIT_H = 8
DONE_KEEP_DAYS = 3
WEEKDAYS = "月火水木金土日"
GITHUB_REF = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/\d+")
IMPORTANCE_ORDER = {"高": 0, "中": 1, "低": 2}


def today():
    return datetime.now(JST).date()


def parse_date(s):
    try:
        return date.fromisoformat(s.strip()) if s and s.strip() else None
    except ValueError:
        return None


def md(d):
    return f"{d.month}/{d.day}" if d else ""


def mdw(d):
    return f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})"


def hours(s):
    try:
        return float(s) if s and s.strip() else 0.0
    except ValueError:
        return 0.0


def fmt_h(h):
    return f"{h:g}h"


def link_url(value):
    """リンク型の列は {"originalUrl": ...} の JSON で返ってくる。"""
    value = (value or "").strip()
    if value.startswith("{"):
        try:
            return json.loads(value).get("originalUrl", "")
        except json.JSONDecodeError:
            return value
    return value


def load_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return parse_rows(f.read())


def parse_rows(text):
    rows = []
    for r in csv.DictReader(line for line in io.StringIO(text, newline="") if not line.startswith("#")):
            name = (r.get("タスク名") or "").strip()
            if not name:
                continue
            rows.append({
                "id": r["Record ID"],
                "name": name,
                "project": (r.get("プロジェクト") or "").strip() or "未分類",
                "status": (r.get("状態") or "").strip() or "未着手",
                "due": parse_date(r.get("期限")),
                "planned": parse_date(r.get("やる予定日")),
                "importance": (r.get("重要度") or "").strip(),
                "hours": hours(r.get("工数（h）")),
                "detail": (r.get("タスクの詳細") or "").strip(),
                "memo": (r.get("その他メモ") or "").strip(),
                "issue": link_url(r.get("issue")),
                "session": link_url(r.get("セッション")),
            })
    return rows


def load_state():
    if LIST_STATE.exists():
        return json.loads(LIST_STATE.read_text())
    return {"records": {}}


def save_state(state):
    LIST_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))


def expectation_flags():
    """台帳で期待値が「高」以上の項目 id。"""
    if not LEDGER.exists():
        return set()
    items = json.loads(LEDGER.read_text()).get("items", [])
    return {i.get("id") for i in items if (i.get("expectation") or {}).get("level") in ("高", "危険")}


def marks(row, state, flagged, now):
    rec = state["records"].get(row["id"], {})
    out = ""
    added = parse_date(rec.get("added_at"))
    if rec.get("source") == "sync" and added and (now - added).days <= 1:
        out += " 🆕"
    if rec.get("ledger_id") in flagged:
        out += " ⚠️"
    return out


def due_text(row, now):
    if not row["due"]:
        return "期限なし"
    estimated = "期限は推定" in row["memo"]
    text = f"〜{md(row['due'])}（推定）" if estimated else md(row["due"])
    if row["due"] < now and row["status"] != DONE:
        text += " 期限切れ"
    return text


def open_rows(rows):
    return [r for r in rows if r["status"] != DONE]


def by_project(rows):
    groups = {}
    for r in rows:
        groups.setdefault(r["project"], []).append(r)
    key = lambda r: (r["due"] or date.max, IMPORTANCE_ORDER.get(r["importance"], 3))
    for g in groups.values():
        g.sort(key=key)
    return sorted(groups.items(), key=lambda kv: kv[1][0]["due"] or date.max)


def recent_done(rows, state, now, days):
    out = []
    for r in rows:
        done_at = parse_date(state["records"].get(r["id"], {}).get("done_at"))
        if r["status"] == DONE and done_at and (now - done_at).days < days:
            out.append((r, done_at))
    return out


def cmd_sync(args):
    with open(args.csv, newline="", encoding="utf-8") as f:
        text = f.read()
    rows = parse_rows(text)
    if not rows:
        # 読めた行が無いまま進むと、状態ファイルの全行が「削除」扱いで消える（JSON のまま渡したときなど）
        sys.exit(f"{args.csv} から行を読めませんでした。slack_read_list の CSV を渡してください")
    # 同期はリストを標準入力で渡すので、todo_sessions.py が読む写しをここで最新にする
    if Path(args.csv).resolve() != CSV_PATH.resolve():
        CSV_PATH.write_text(text, encoding="utf-8")
    state = load_state()
    now = today()
    seen = {r["id"] for r in rows}
    changes = {"done": [], "reopened": [], "deleted": []}

    for r in rows:
        rec = state["records"].setdefault(r["id"], {"source": "user", "first_seen": now.isoformat()})
        if r["status"] == DONE and not rec.get("done_at"):
            rec["done_at"] = now.isoformat()
            changes["done"].append({"id": r["id"], "name": r["name"], "ledger_id": rec.get("ledger_id")})
        elif r["status"] != DONE and rec.get("done_at"):
            rec.pop("done_at")
            changes["reopened"].append({"id": r["id"], "name": r["name"], "ledger_id": rec.get("ledger_id")})

    for rid in [rid for rid in state["records"] if rid not in seen]:
        rec = state["records"].pop(rid)
        changes["deleted"].append({"id": rid, "ledger_id": rec.get("ledger_id")})

    save_state(state)
    TODO.write_text(render_todo(rows, state, now))
    print(json.dumps(changes, ensure_ascii=False, indent=1))


def render_todo(rows, state, now):
    flagged = expectation_flags()
    lines = [
        "# ToDo（本人の作業リスト）",
        "",
        "Slack リスト「ToDo ボード」（state/todo-list.json）が正。毎時の同期がリストを読んで、このファイルを作り直す。",
        "ここを手で直しても次の同期で消える。直すのはリストで（同期が足すときもリストに行を足す）。",
        f"最終更新: {datetime.now(JST).strftime('%Y-%m-%d %H:%M')}",
        "",
        "## タスク",
    ]
    for project, items in by_project(open_rows(rows)):
        lines.append(f"- {project}")
        for r in items:
            meta = [due_text(r, now)]
            if r["planned"]:
                meta.append(f"予定 {md(r['planned'])}")
            if r["hours"]:
                meta.append(f"工数 {fmt_h(r['hours'])}")
            head = f"[{r['status']}]" + (f"[{r['importance']}]" if r["importance"] else "")
            lines.append(f"    - {head} {r['name']} — {' ｜ '.join(meta)}{marks(r, state, flagged, now)}")
            for label, key in (("詳細", "detail"), ("メモ", "memo"), ("issue", "issue")):
                if r[key]:
                    lines.append(f"        - {label}：{r[key]}")
    lines += ["", f"## 済み（{DONE_KEEP_DAYS} 日で消す。リストからは消えないので、要らなければリストで手で消す）"]
    for r, done_at in recent_done(rows, state, now, DONE_KEEP_DAYS):
        lines.append(f"- {r['project']}：{r['name']}（{md(done_at)} にリストで済み）")
    return "\n".join(lines) + "\n"


def cmd_mark_added(args):
    state = load_state()
    rec = state["records"].setdefault(args.record_id, {"first_seen": today().isoformat()})
    rec.update({"source": "sync", "ledger_id": args.ledger_id, "added_at": today().isoformat()})
    save_state(state)


def cmd_canvas(args):
    rows = load_rows(args.csv)
    state = load_state()
    now = today()
    links = json.loads((STATE / "todo-list.json").read_text())
    pending = open_rows(rows)
    out = [
        "::: {.callout}\n"
        f"見るための集計です（毎時の同期が書き直すので、ここでの編集は残りません）。タスクの追加・変更は [リスト]({links['url']}) で。"
        f"最終更新 {datetime.now(JST).strftime('%m/%d %H:%M')}\n:::",
    ]

    todays = sorted([r for r in pending if r["planned"] == now], key=lambda r: IMPORTANCE_ORDER.get(r["importance"], 3))
    total = sum(r["hours"] for r in todays)
    warn = " ⚠️" if total > DAY_LIMIT_H else ""
    out.append(f"## 今日 {mdw(now)} の予定　計 {fmt_h(total)}{warn}")
    out.append("\n".join(f"* [{r['importance'] or '-'}] {r['name']} — {due_text(r, now)}（{fmt_h(r['hours'])}）" for r in todays)
               or "予定日が今日のタスクはありません")

    late = sorted([r for r in pending if r["due"] and r["due"] <= now], key=lambda r: r["due"])
    if late:
        out.append("## 期限が今日まで・期限切れ")
        out.append("\n".join(f"* {r['project']}：{r['name']} — {due_text(r, now)}" for r in late))

    out.append("## 予定日ごとの工数（今日から 14 日）")
    table = ["| 日付 | 工数 | 件数 | 中身 |", "|---|---|---|---|"]
    for i in range(14):
        d = now + timedelta(days=i)
        items = [r for r in pending if r["planned"] == d]
        if not items:
            continue
        h = sum(r["hours"] for r in items)
        flag = " ⚠️" if h > DAY_LIMIT_H else ""
        names = "<br>".join(f"{r['name'][:30]}（{fmt_h(r['hours'])}）" for r in items)
        table.append(f"| {mdw(d)} | {fmt_h(h)}{flag} | {len(items)} | {names} |")
    overdue_plan = [r for r in pending if r["planned"] and r["planned"] < now]
    if overdue_plan:
        table.append(f"| 予定日を過ぎた | {fmt_h(sum(r['hours'] for r in overdue_plan))} | {len(overdue_plan)} | "
                     + "<br>".join(r["name"][:30] for r in overdue_plan) + " |")
    out.append("\n".join(table))

    out.append("## プロジェクトごとの残り工数")
    table = ["| プロジェクト | 残り工数 | 件数 | いちばん早い期限 |", "|---|---|---|---|"]
    for project, items in by_project(pending):
        first_due = min((r["due"] for r in items if r["due"]), default=None)
        table.append(f"| {project} | {fmt_h(sum(r['hours'] for r in items))} | {len(items)} | {md(first_due) or 'なし'} |")
    table.append(f"| 合計 | {fmt_h(sum(r['hours'] for r in pending))} | {len(pending)} | |")
    out.append("\n".join(table))

    unplanned = [r for r in pending if not r["planned"]]
    if unplanned:
        out.append("## 予定日が未定")
        out.append("\n".join(f"* {r['project']}：{r['name']} — {due_text(r, now)}" for r in unplanned))
    body = "\n\n".join(out)
    if args.if_changed:
        strip = lambda t: "\n".join(l for l in t.splitlines() if "最終更新" not in l)
        if CANVAS_BODY.exists() and strip(CANVAS_BODY.read_text()) == strip(body):
            print("UNCHANGED")
            return
        CANVAS_BODY.write_text(body)
    print(body)


def cmd_digest(args):
    rows = load_rows(args.csv)
    state = load_state()
    now = today()
    flagged = expectation_flags()
    links = json.loads((STATE / "todo-list.json").read_text())
    canvas = json.loads((STATE / "todo-canvas.json").read_text())
    pending = open_rows(rows)
    todays = [r for r in pending if r["planned"] == now]
    total = sum(r["hours"] for r in todays)
    out = [f":sunrise: タスク一覧（{mdw(now)} 8 時時点）",
           f"今日の予定工数 {fmt_h(total)}（{len(todays)} 件）" + (" ⚠️ 8h 超え" if total > DAY_LIMIT_H else ""), ""]
    for project, items in by_project(pending):
        out.append(f"*{project}*")
        for r in items:
            planned = f"（予定 {md(r['planned'])}）" if r["planned"] else ""
            out.append(f"・{r['name']} — {due_text(r, now)}{planned}{marks(r, state, flagged, now)}")
        out.append("")
    done = recent_done(rows, state, now, 2)
    if done:
        out.append(":white_check_mark: 昨日済んだこと")
        out += [f"・{r['project']}：{r['name']}" for r, _ in done]
        out.append("")
    out.append(f":memo: 編集はリスト {links['url']} ／ 集計はキャンバス {canvas['url']}")
    print("\n".join(out))


def cmd_issues(args):
    """issue の列（リンクは1つしか入らない）と、詳細に書いた2つ目以降の issue・PR の URL を出す。"""
    for r in open_rows(load_rows(args.csv)):
        urls = list(dict.fromkeys(GITHUB_REF.findall(" ".join((r["issue"], r["detail"])))))
        if urls:
            print(f"{r['id']}\t{' '.join(urls)}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--csv", default=str(CSV_PATH))
    sub = p.add_subparsers(required=True)
    sub.add_parser("sync").set_defaults(func=cmd_sync)
    m = sub.add_parser("mark-added")
    m.add_argument("record_id")
    m.add_argument("ledger_id")
    m.set_defaults(func=cmd_mark_added)
    c = sub.add_parser("canvas")
    c.add_argument("--if-changed", action="store_true", help="前回書いた本文と同じなら UNCHANGED だけ出す（最終更新の行は比べない）")
    c.set_defaults(func=cmd_canvas)
    sub.add_parser("digest").set_defaults(func=cmd_digest)
    sub.add_parser("issues").set_defaults(func=cmd_issues)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())

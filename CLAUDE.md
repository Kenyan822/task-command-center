# 司令塔ディレクトリ

案件をまたいで状況を把握し、各プロジェクトに作業を振り分けるための場所。コードは置かない。
デスクトップ・書類・ダウンロードの下は macOS の保護で launchd から起動できないため、ホーム直下などに置く。個人の設定は `config.json`。

## セッション開始時
- まず `state/snapshot.md` を読む。これが Slack・GitHub・各プロジェクトの Claude Code セッション・Orca から集めた最新の状況（毎日 5〜24 時に1時間ごと、`bin/sync.sh` が更新している。作業リストの正は Slack リスト「ToDo ボード」（`state/todo-list.json`）で、本人はそこで足す・直す・済みにする。`state/todo.md` は毎時の同期がリストから作り直す写し。自分宛て DM のキャンバス（`state/todo-canvas.json`）は予定日ごと・プロジェクトごとの工数の集計で、見る専用。毎朝 8 時の朝のまとめ（本人 DM）はその時点の写し。`/todo`（作り直し）と `/nippo` は頼まれたときだけ手動で回す）
- 最終同期から1時間以上経っていたら、その旨を伝える。必要なら `/sync` を手動で回す

## 構成
- `.claude/skills/sync/` — 同期と期待値ギャップ検知の手順
- `bin/sync.sh` — launchd から呼ばれる定期実行の入口（`launchd/sync.plist.example` を写して登録する）
- `bin/cc_recent.py` — 各プロジェクトの Claude Code セッション履歴から要点を抜き出す
- `state/ledger.json` — 依頼・約束の台帳。`state/snapshot.md` — 人が読む現状のまとめ
- `state/feedback.md` — 期待値判定への本人の補正
- `bin/todo_list.py` — ToDo リストの CSV から todo.md・キャンバスの集計・朝の DM を作る。行の出入りと済みにした日は `state/todo-list-state.json`
- `logs/` — 定期実行のログ

## 運用
- 本人が判定に補正を入れたら（「その件は大丈夫」「この人は急かさない」など）、`state/feedback.md` に日付つきで追記する。次の同期から反映される
- 「〇〇の件を走らせて」と言われたら、台帳と snapshot の該当箇所を材料に依頼文を書き、Orca で対象リポジトリにセッションを立てる（`orca worktree create --repo id:<repoId> --agent claude --prompt ...`）。立てる前に依頼文を見せて承認をもらう。例外は ToDo の当日のコードのタスクで、`bin/todo_sessions.py`（launchd で常駐。Orca を使う場合だけ）が承認なしで worktree を作り、権限を絞った Claude に環境構築だけさせる。リストの「セッション」の列のリンク（localhost:47615）を押すと、その端末を Orca で開く（閉じていれば `claude --resume` で再開する）
- 自分宛て以外への Slack 送信は、本文を見せて承認をもらってから

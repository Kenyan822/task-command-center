# pm-command-center

案件をいくつもかけ持ちする PM のための「司令塔ディレクトリ」。Claude Code を 1 時間ごとに headless で回し、Slack・Gmail・GitHub・各プロジェクトの Claude Code セッションから「誰に何を約束したか」を集めて台帳にし、**相手の期待値が上がりすぎている件だけ**を自分宛ての Slack DM で知らせる。

参考実装として公開しているもので、動作の保証やサポートはしない。自分の環境に合わせて書き換えて使う前提。

## できること
- **/sync**（毎時）：メンション・DM・自分の発言・リアクション・Gmail・GitHub の issue/PR・Claude Code のセッション履歴を集め、`state/ledger.json`（約束の台帳）を更新する。期待値のずれを 低・中・高・危険 で判定し、「高」以上に上がった件だけ DM で知らせる。朝 8 時台には ToDo の一覧を DM で送る
- **ToDo ボード**：作業リストの正は Slack リスト。同期が台帳から行を足し、終わった証拠（PR のマージ、スレッドの決着など）がある行を「済み」にする。`bin/todo_list.py` がリストの CSV から写し（`state/todo.md`）・予定日ごとの工数の集計（キャンバス）・朝の DM を作る
- **/nippo**：その日の活動から日報の下書きを作り、自分宛て DM に送る
- **作業環境の自動準備**（任意・[Orca](https://github.com/stablyai/orca) を使う場合）：予定日が今日で issue が付いた ToDo ごとに worktree を作り、権限を絞った Claude に環境構築だけさせる

## 安全のための線引き
- 定期実行中の Slack への書き込みは、自分宛て DM・下書き・ToDo のリストとキャンバスだけ。`.claude/hooks/guard_slack.py` が PreToolUse hook で止める
- headless の Claude は `bin/sync.sh` の `--allowedTools` に列挙した操作しかできない
- `SYNC_DRY_RUN=1` で動かすと、Slack には何も書かず `state/outbox-*.md` に書く。最初はこれで試す

## 必要なもの
- macOS（定期実行に launchd を使う）
- [Claude Code](https://claude.com/claude-code)。claude.ai の Slack コネクタと Gmail コネクタをつないでおく
- `gh`（GitHub CLI。ログイン済み）、Python 3.10 以上（標準ライブラリだけで動く）
- Orca（任意。`bin/todo_sessions.py` を使う場合だけ）

## セットアップ
1. ホーム直下などに clone する（デスクトップ・書類の下は launchd から起動できない）
2. `cp config.example.json config.json` で設定を作る

   | キー | 中身 |
   |---|---|
   | `slack_user_id` | 自分の Slack の user ID（プロフィール →「メンバー ID をコピー」） |
   | `github_owner` | issue・PR を見る GitHub の org（またはユーザー） |
   | `gmail_account` | 読むメールアカウント |
   | `nippo_channel_id` | 日報を出すチャンネル（/nippo を使う場合だけ） |
   | `cc_projects_root` | 各プロジェクトを置いているディレクトリ。この下で動いた Claude Code のセッションを集める |

3. Slack に ToDo のリストを作る。列の名前は次のとおりにする（`bin/todo_list.py` が名前で読む）

   | 列 | 種類 |
   |---|---|
   | タスク名 | テキスト |
   | プロジェクト | 選択（案件名） |
   | 状態 | 選択（未着手・進行中・済み） |
   | 期限・やる予定日 | 日付 |
   | 重要度 | 選択（高・中・低） |
   | 工数（h） | 数値 |
   | タスクの詳細・その他メモ | テキスト |
   | issue・セッション | リンク |

4. 自分宛て DM にキャンバスを 1 枚作り、タイトルの節だけ `# ToDo` にしておく
5. `state/` を作り、リストとキャンバスの ID を書く

   ```sh
   mkdir -p state logs
   echo '{"list_id": "F0XXXXXXXXX"}' > state/todo-list.json
   echo '{"canvas_id": "F0XXXXXXXXX"}' > state/todo-canvas.json
   cp feedback.example.md state/feedback.md
   ```

6. このディレクトリで `claude` を起動し、一度対話で `/sync` を回す。コネクタの許可や、各ツールの動きをここで確かめる
7. dry run で headless を試す：`SYNC_DRY_RUN=1 bash bin/sync.sh`。`logs/` と `state/outbox-sync.md` を見る
8. 定期実行を登録する（自分の手で）

   ```sh
   sed -e "s#__ROOT__#$PWD#g" launchd/sync.plist.example > ~/Library/LaunchAgents/local.pm-command-center.sync.plist
   launchctl load ~/Library/LaunchAgents/local.pm-command-center.sync.plist
   ```

   Orca を使う場合は `launchd/todo-sessions.plist.example` も同じように（`__HOME__` も置き換える）。

## 使い方
- 作業リストは Slack のリストで足す・直す・済みにする。`state/todo.md` は写しなので手で直さない
- 判定がずれていたら、Claude に「その件は大丈夫」「この人は急かさない」と言う。`state/feedback.md` に追記され、次の同期から反映される（書き方は `feedback.example.md`）
- 状況を聞きたいときは、このディレクトリで `claude` を起動する。`CLAUDE.md` の指示で `state/snapshot.md` を最初に読む

## 構成
```
bin/sync.sh            launchd から呼ばれ、claude -p "/sync" を許可した操作だけで回す
bin/cc_recent.py       各プロジェクトの Claude Code セッション履歴から要点を抜き出す
bin/todo_list.py       ToDo リストの CSV から todo.md・キャンバス・朝の DM を作る
bin/todo_sessions.py   ToDo ごとの worktree の準備（Orca を使う場合）
.claude/skills/        sync・todo・nippo の手順（Claude が読む）
.claude/hooks/         定期実行中の Slack への書き込みを制限する hook
state/                 台帳・現状のまとめ・リストの写し（git には入れない）
```

skill の手順は日本語の Slack ワークスペース・日本語のやり取りを前提に書いている。言い回し（「やります」「確認します」で引き受けを拾うなど）は自分の環境に合わせて直す。

## ライセンス
MIT

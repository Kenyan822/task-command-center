#!/bin/bash
# launchd から1時間ごとに呼ばれ、/sync を headless で実行する。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export SYNC_HEADLESS=1

cd "$ROOT"
mkdir -p logs state
LOG="logs/sync-$(date +%Y%m%d).log"

# 前回の実行が終わっていなければ重ねて走らせない
LOCK="state/.sync.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date '+%F %T') skip: previous run still active" >> "$LOG"
  exit 0
fi
trap 'rmdir "$LOCK"' EXIT

ALLOWED=(
  Read Write Edit Glob Grep
  "Bash(date:*)"
  "Bash(python3 bin/cc_recent.py:*)" "Bash(python3 bin/todo_list.py:*)" "Bash(gh issue view:*)"
  "Bash(python3 bin/todo_sessions.py targets:*)" "Bash(python3 bin/todo_sessions.py stale:*)"
  "Bash(gh search prs:*)" "Bash(gh search issues:*)" "Bash(gh pr view:*)"
  "Bash(orca worktree ps:*)"
  mcp__claude_ai_Slack__slack_search_public_and_private
  mcp__claude_ai_Slack__slack_search_public
  mcp__claude_ai_Slack__slack_read_channel
  mcp__claude_ai_Slack__slack_read_thread
  mcp__claude_ai_Slack__slack_get_reactions
  mcp__claude_ai_Slack__slack_read_canvas
  mcp__claude_ai_Slack__slack_list_user_channels
  mcp__claude_ai_Slack__slack_read_user_profile
  mcp__claude_ai_Slack__slack_search_users
  mcp__claude_ai_Gmail__search_threads
  mcp__claude_ai_Gmail__get_thread
  mcp__claude_ai_Gmail__list_drafts
  mcp__claude_ai_Slack__slack_send_message
  mcp__claude_ai_Slack__slack_send_message_draft
  mcp__claude_ai_Slack__slack_update_canvas
  mcp__claude_ai_Slack__slack_read_list
  mcp__claude_ai_Slack__slack_add_list_record
  mcp__claude_ai_Slack__slack_update_list_record
  mcp__claude_ai_Slack__slack_update_list
)

PROMPT="${SYNC_PROMPT:-/sync}（SYNC_DRY_RUN=${SYNC_DRY_RUN:-0}）"

echo "$(date '+%F %T') start dry_run=${SYNC_DRY_RUN:-0} prompt=$PROMPT" >> "$LOG"
claude -p "$PROMPT" \
  --model claude-opus-5-5 \
  --allowedTools "${ALLOWED[@]}" \
  --output-format text >> "$LOG" 2>&1 || {
    echo "$(date '+%F %T') FAILED exit=$?" >> "$LOG"
    osascript -e "display notification \"$ROOT/$LOG を確認してください\" with title \"sync 失敗\""
  }
echo "$(date '+%F %T') end" >> "$LOG"

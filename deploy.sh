#!/usr/bin/env zsh
# deploy.sh — ローカルプレビュー起動 + GitHub push を同時実行
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
DOCS_DIR="$REPO_DIR/docs"
PORT=8766

# ── コミットメッセージ ──────────────────────────────────
MSG="${1:-update: docs}"

# ── 1. ローカルサーバー起動（既存プロセスを差し替え） ──
echo "\n🌐  ローカルサーバーを起動中 (port $PORT)..."
pkill -f "http-server.*$PORT" 2>/dev/null || true
sleep 0.3
npx --yes http-server "$DOCS_DIR" -p $PORT --cors -c-1 --silent &
SERVER_PID=$!

# サーバーが立ち上がるまで待機（最大3秒）
for i in {1..6}; do
  curl -s -o /dev/null "http://localhost:$PORT/" && break
  sleep 0.5
done

echo "   → http://localhost:$PORT/ を開きます"
open "http://localhost:$PORT/"

# ── 2. GitHub push ─────────────────────────────────────
# リモートリポジトリが設定されている場合のみ実行
if git remote | grep -q 'origin'; then
  echo "\n🚀  GitHub へ push 中..."
  cd "$REPO_DIR"
  
  # 必要なソースファイルのみ git add（存在しないファイルがあっても失敗しないよう個別に追加）
  git add docs/ 2>/dev/null || true
  git add .gitignore deploy.sh firebase.json .firebaserc 2>/dev/null || true
  git add README.md 2>/dev/null || true
  git add *.py 2>/dev/null || true
  
  if git diff --cached --quiet; then
    echo "   変更なし — push をスキップしました"
  else
    git commit -m "$MSG"
    git push
    echo "   ✅  push 完了！"
  fi
else
  echo "\n⚠️  GitHub リモートリポジトリ (origin) が未設定のため、push はスキップされました。"
  echo "設定するには: git remote add origin <URL>"
fi

echo "\nローカルサーバーは動き続けています (PID: $SERVER_PID)"
echo "停止するには: kill $SERVER_PID  または pkill -f 'http-server.*$PORT'"

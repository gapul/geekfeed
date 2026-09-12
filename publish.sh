#!/bin/sh
# Collect, regenerate public/, and push. Cloudflare Pages builds from the pushed commit.
set -e
cd "$(dirname "$0")"
export PATH=/run/current-system/sw/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin

[ -f "$HOME/.config/geekfeed/claude.token" ] &&
  CLAUDE_CODE_OAUTH_TOKEN=$(cat "$HOME/.config/geekfeed/claude.token") && export CLAUDE_CODE_OAUTH_TOKEN

/usr/bin/python3 geekfeed.py "$@" < /dev/null

git add -A public
git diff --cached --quiet || {
  git commit -q -m "feed: $(date '+%Y-%m-%d %H:%M')"
  git push -q origin main
}

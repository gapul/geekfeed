#!/bin/sh
# Collect, regenerate public/, and push. Cloudflare Pages builds from the pushed commit.
set -e
cd "$(dirname "$0")"
# launchd は素の PATH で来るので、git と gh のある nix プロファイルを明示的に足す。
export PATH=/etc/profiles/per-user/$USER/bin:/run/current-system/sw/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin

[ -f "$HOME/.config/geekfeed/claude.token" ] &&
  CLAUDE_CODE_OAUTH_TOKEN=$(cat "$HOME/.config/geekfeed/claude.token") && export CLAUDE_CODE_OAUTH_TOKEN

/usr/bin/python3 geekfeed.py "$@" < /dev/null

git add -A public
git diff --cached --quiet || {
  git commit -q -m "feed: $(date '+%Y-%m-%d %H:%M')"
  git push -q origin main
}

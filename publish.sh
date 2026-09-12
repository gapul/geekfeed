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

  # Cloudflare Pages は GitHub 連携で作ってあるが、CF の GitHub App がこのリポジトリに
  # 未許可なので push の webhook が来ない。だから push 後に自分でデプロイを叩く。
  # App にこのリポジトリを許可すれば、この節とトークンは消せる。
  cf="$HOME/.config/geekfeed/cloudflare.token"   # 1行目: API トークン / 2行目: account id
  if [ -f "$cf" ]; then
    curl -fsS -o /dev/null -X POST \
      -H "Authorization: Bearer $(sed -n 1p "$cf")" \
      -H "Content-Type: application/json" \
      --data '{"branch":"main"}' \
      "https://api.cloudflare.com/client/v4/accounts/$(sed -n 2p "$cf")/pages/projects/geekfeed/deployments" &&
      echo "pages: deploy triggered"
  fi
}

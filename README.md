# geekfeed

ギーク情報と、学生向けの無料・学割キャンペーンを毎日集めて **RSS** と **ICS カレンダー** で配る。

- サイト: <https://geekfeed.pages.dev/>
- RSS: <https://geekfeed.pages.dev/feed.xml>
- カレンダー: <https://geekfeed.pages.dev/events.ics>（購読すると締切が予定として入る）

## 仕組み

```
macmini (launchd, 1日2回)
  └─ publish.sh
       ├─ geekfeed.py            ← 20 本ほどの RSS/Atom + Google ニュース検索
       │   ├─ Miniflux (rss.gapul.net) の購読エントリも入力にする
       │   ├─ --research なら claude -p にウェブ調査させて公式発表を拾う
       │   ├─ キャンペーンだけ本文を読んで締切日を抽出
       │   └─ public/{feed.xml, events.ics, index.html, items.json} を生成
       └─ git push → Cloudflare Pages が公開
```

判定は「学生」×「無料・学割」×「ソフト/AI/サービス」の3語が揃ったものだけを
キャンペーン扱いにする。飲食店の学割や就活イベントを落とすためで、ここを緩めると
カレンダーが埋まる。

## 使い方

```sh
./publish.sh              # フィードだけ（安い・速い）
./publish.sh --research   # Claude のウェブ調査つき（1日1回想定）
python3 geekfeed.py --selftest
```

## 必要なもの

| ファイル | 中身 |
|---|---|
| `~/.config/geekfeed/miniflux.token` | Miniflux の API キー（購読の読み込みと自動購読に使う） |
| `~/.config/geekfeed/claude.token` | `CLAUDE_CODE_OAUTH_TOKEN`（`--research` のときだけ） |

どちらも無ければその機能だけ黙って飛ばす。Python は標準ライブラリのみ。

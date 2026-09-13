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
       └─ git push → Cloudflare Pages のデプロイを API で叩く
```

## ペルソナ

対象の基準は [`persona.md`](persona.md) に置いてある。調査プロンプトにそのまま渡し、
**ここから外れるものは載せない**（米国限定・K-12 限定・他大学限定など）。条件が変わったら
このファイルだけ直せばよい。キーワード側でも `INELIGIBLE_RE` が同じ基準で捨てる。

読者像を渡すことで、認証方法（大学メール / SheerID / ISIC）の記載と、
**大学単位でしか開放されないもの**（GMO 天秤AI for UTokyo、学内の LinkedIn Learning や
GPU クラスタ SYNKA、東大生協の特典など）が拾えるようになる。ここが手で追うと最も漏れる。
既に載っている特典の一覧も毎回プロンプトに渡して、同じ常設オファーの再提出を防いでいる。

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
| `~/.config/geekfeed/cloudflare.token` | 1行目に API トークン、2行目に account id（Pages のデプロイを叩くため） |

どれも無ければその機能だけ黙って飛ばす。Python は標準ライブラリのみ。

Cloudflare の GitHub App にこのリポジトリを許可すれば push で勝手にデプロイされるので、
`cloudflare.token` と publish.sh の curl は消せる。

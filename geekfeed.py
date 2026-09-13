#!/usr/bin/env python3
"""geekfeed - aggregate geek news and student/free campaign offers into RSS + ICS.

Stdlib only (runs on macOS system python3). Fetches a curated set of feeds,
keeps a rolling item store, and regenerates public/feed.xml + public/events.ics.
"""

import concurrent.futures
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime

UA = "geekfeed/1.0 (+https://github.com/gapul)"
OUT = os.environ.get("GEEKFEED_OUT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")
STATE = os.path.join(OUT, "items.json")
KEEP = 400          # items retained in the store
PER_SOURCE = 30     # items taken per fetch, per source
SITE = os.environ.get("GEEKFEED_SITE", "https://geekfeed.pages.dev/")


def gnews(q, lang="ja"):
    p = urllib.parse.quote(q)
    if lang == "ja":
        return f"https://news.google.com/rss/search?q={p}+when:7d&hl=ja&gl=JP&ceid=JP:ja"
    return f"https://news.google.com/rss/search?q={p}+when:7d&hl=en-US&gl=US&ceid=US:en"


def hatena(q):
    return "https://b.hatena.ne.jp/q/%s?mode=rss&sort=recent&users=3" % urllib.parse.quote(q)


VENDORS = ("ElevenLabs OR Perplexity OR Notion OR Figma OR Cursor OR GitHub OR JetBrains "
           "OR Canva OR Adobe OR Anthropic OR OpenAI OR Gemini OR Claude")

SOURCES = [
    # ギーク情報
    ("Hacker News", "https://hnrss.org/frontpage"),
    ("Lobsters", "https://lobste.rs/rss"),
    ("GitHub Blog", "https://github.blog/feed/"),
    ("はてブ テクノロジー", "https://b.hatena.ne.jp/hotentry/it.rss"),
    ("Publickey", "https://www.publickey1.jp/atom.xml"),
    ("gihyo.jp", "https://gihyo.jp/feed/atom"),
    ("Zenn", "https://zenn.dev/feed"),
    ("Qiita", "https://qiita.com/popular-items/feed"),
    ("InfoQ", "https://feed.infoq.com/"),
    ("JetBrains", "https://blog.jetbrains.com/feed/"),
    ("OpenAI", "https://openai.com/news/rss.xml"),
    # 学生向け無料キャンペーン
    ("GitHub Education", "https://github.blog/tag/github-education/feed/"),
    ("HN: student plan", 'https://hnrss.org/newest?q=%22free+for+students%22+OR+%22student+plan%22+OR+%22student+pack%22&count=30'),
    ("Google News: 学生無料ツール", gnews("学生 (無料 OR 無償) (ライセンス OR プラン OR ツール OR ソフト OR AI)")),
    ("Google News: 学割サブスク", gnews("学割 (プラン OR サブスク OR ライセンス OR アプリ OR AI)")),
    ("Google News: 教育無償提供", gnews("(教育機関 OR 学生) 向け 無償提供 OR 無料開放")),
    ("Google News: ベンダー学生", gnews("(%s) 学生 無料" % VENDORS)),
    ("Google News: student free plan", gnews('"free for students" OR "student plan" OR "student discount" (AI OR software OR developer)', "en")),
    ("Google News: student pack", gnews('"student developer pack" OR "free student license" OR "students get free"', "en")),
    ("Google News: 東大", gnews("(東京大学 OR 大学生協) 学生 (無償 OR 無料 OR ライセンス)")),
    ("はてブ: 学生 無料", hatena("学生 無料")),
    ("はてブ: 学割", hatena("学割")),
]

# 「学生」×「無料」×「ソフト/サービス」の3条件が揃ったものだけキャンペーン扱いにする。
# Google ニュース検索は学食やライブの学割まで拾うので、この3点を要求しないとカレンダーが埋まる。
# 「大学」も入れる。tenbin.ai のような大学単位の無料開放は「学生」と書かずに大学名だけで報じられる。
STUDENT_RE = re.compile(r"学生|学割|大学|高専|高校生|生徒|在学|教育|student|academic|campus|university|college|education", re.I)
OFFER_RE = re.compile(r"無料|無償|タダ|学割|割引|プレゼント|開放|free|no cost|discount|giveaway|credits?|waiv", re.I)
TECH_RE = re.compile(  # 「プラン」単体は入れない。ライフプラン/宿泊プランで旅行・セミナーが大量に紛れ込む。
    r"AI|LLM|ソフト|アプリ|ツール|ライセンス|サブスク|アカウント|API|クラウド|開発|エディタ|"
    r"プログラミング|デベロッパー|software|app\b|license|subscription|developer|premium|"
    r"cloud|coding|IDE|tool|platform|GPU|SaaS", re.I)
BLOCK_RE = re.compile(r"求人|採用|新卒|就活|説明会|入試|奨学金|禁止|逮捕|ライブ|ツアー|コンサート|"
                      r"lawsuit|\bban\b|arrest|concert|tour date", re.I)

# persona.md の「載せないもの」に対応する。申し込めないキャンペーンは news に落とさず捨てる。
INELIGIBLE_RE = re.compile(
    r"米国(?:の学生|の大学生)?(?:限定|のみ|在住)|アメリカ(?:限定|のみ)|US[-\s]?only|only in the (?:US|United States)|"
    r"対象外|K-?12|小中高|中高生限定|高校生限定|教員限定|教職員限定|teachers? only|faculty only", re.I)


def classify(text):
    """news | deal | skip。skip はペルソナの対象外なので保存も配信もしない。"""
    offer = STUDENT_RE.search(text) and OFFER_RE.search(text)
    if offer and INELIGIBLE_RE.search(text):
        return "skip"  # 申し込めない特典は、ニュースとしても出さない
    if BLOCK_RE.search(text):
        return "news"
    if offer and TECH_RE.search(text):
        return "deal"
    return "news"

# Only trust a date when a deadline-ish word sits nearby; otherwise the publish date wins.
DEADLINE_RE = re.compile(r"締切|締め切り|期限|まで|終了|受付|応募|deadline|until|through|expires?|ends?|by\b", re.I)

MONTHS = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}

DATE_PATTERNS = [
    (re.compile(r"(\d{4})\s*[年/-]\s*(\d{1,2})\s*[月/-]\s*(\d{1,2})"), "ymd"),
    (re.compile(r"(?<![\d年])(\d{1,2})\s*月\s*(\d{1,2})\s*日"), "md"),  # 年つきは上の ymd で拾う
    (re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})?", re.I), "mdy"),
    (re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s*(\d{4})?", re.I), "dmy"),
]


def strip_html(s):
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def norm_url(u):
    try:
        p = urllib.parse.urlsplit(u)
    except ValueError:
        return u
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if not k.startswith(("utm_", "ref"))]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path.rstrip("/") or "/", urllib.parse.urlencode(q), ""))


def parse_when(s):
    if not s:
        return None
    s = s.strip()
    try:
        return parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def find_deadline(text, today=None):
    """First plausible future date sitting next to a deadline-ish word."""
    today = today or date.today()
    limit = today + timedelta(days=550)
    best = None
    for rx, kind in DATE_PATTERNS:
        for m in rx.finditer(text):
            window = text[max(0, m.start() - 40):m.end() + 20]
            if not DEADLINE_RE.search(window):
                continue
            try:
                if kind == "ymd":
                    d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                elif kind == "md":
                    mo, day = int(m.group(1)), int(m.group(2))
                    d = date(today.year, mo, day)
                    if d < today:
                        d = date(today.year + 1, mo, day)
                elif kind == "mdy":
                    y = int(m.group(3)) if m.group(3) else today.year
                    d = date(y, MONTHS[m.group(1)[:3].lower()], int(m.group(2)))
                else:
                    y = int(m.group(3)) if m.group(3) else today.year
                    d = date(y, MONTHS[m.group(2)[:3].lower()], int(m.group(1)))
            except ValueError:
                continue
            if today <= d <= limit and (best is None or d < best):
                best = d
    return best


def fetch(url, timeout=20, tries=2):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, */*"})
    for attempt in range(1, tries + 1):  # hnrss と Google ニュースはたまに素で落ちるので引き直す
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(2_000_000)
        except Exception:
            if attempt == tries:
                raise


def tag(el):
    return el.tag.rsplit("}", 1)[-1]


def parse_feed(blob):
    """RSS 2.0 / RDF / Atom -> [{title, link, summary, published}] (namespace agnostic)."""
    root = ET.fromstring(blob)
    out = []
    for el in root.iter():
        if tag(el) not in ("item", "entry"):
            continue
        item = {"title": "", "link": "", "summary": "", "published": ""}
        for c in el:
            t, txt = tag(c), (c.text or "").strip()
            if t == "title":
                item["title"] = strip_html(txt)
            elif t == "link":
                href = c.get("href")
                if href:
                    if c.get("rel", "alternate") == "alternate":
                        item["link"] = href
                elif txt:
                    item["link"] = txt
            elif t in ("description", "summary", "content", "encoded") and not item["summary"]:
                item["summary"] = strip_html(txt or "".join(c.itertext()))
            elif t in ("pubDate", "published", "updated", "date") and not item["published"]:
                item["published"] = txt
        if item["title"] and item["link"]:
            out.append(item)
    return out[:PER_SOURCE]


def collect():
    items, errors = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(fetch, url): name for name, url in SOURCES}
        for f in concurrent.futures.as_completed(futs):
            name = futs[f]
            try:
                parsed = parse_feed(f.result())
            except Exception as e:  # one bad source must not sink the run
                errors.append("%s: %s" % (name, e))
                continue
            for it in parsed:
                it["source"] = name
                it["kind"] = classify(it["title"] + " " + it["summary"])
                if it["kind"] != "skip":  # ペルソナの対象外は保存しない
                    items.append(it)
    for e in errors:
        print("warn: " + e, file=sys.stderr)
    return items


MINIFLUX = os.environ.get("MINIFLUX_URL", "https://rss.gapul.net").rstrip("/")
TOKENS = os.path.expanduser("~/.config/geekfeed")


def token(name, env):
    v = os.environ.get(env)
    if v:
        return v.strip()
    path = os.path.join(TOKENS, name)
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    return ""


def miniflux(path, body=None):
    tok = token("miniflux.token", "MINIFLUX_TOKEN")
    if not tok:
        raise RuntimeError("no miniflux token (%s/miniflux.token)" % TOKENS)
    req = urllib.request.Request(
        MINIFLUX + path, data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Auth-Token": tok, "Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def from_miniflux(limit=150):
    """自分の Miniflux の購読も入力にする。読んでいるものが deal 判定にもかかる。"""
    try:
        data = miniflux("/v1/entries?direction=desc&order=published_at&limit=%d" % limit)
    except Exception as e:
        print("warn: miniflux read failed: %s" % e, file=sys.stderr)
        return []
    out = []
    for e in data.get("entries", []):
        if not e.get("url") or not e.get("title"):
            continue
        summary = strip_html(e.get("content") or "")[:600]
        kind = classify(e["title"] + " " + summary)
        if kind == "skip":
            continue
        out.append({"title": e["title"], "link": e["url"], "summary": summary,
                    "source": "RSS: " + (e.get("feed") or {}).get("title", "miniflux"),
                    "published": e.get("published_at", ""), "kind": kind})
    return out


def ensure_subscribed(feed_url):
    """自分の Miniflux に geekfeed 自身を購読させる（既にあれば何もしない）。"""
    try:
        if any(f.get("feed_url") == feed_url for f in miniflux("/v1/feeds")):
            return False
        cats = miniflux("/v1/categories")
        cat = next((c for c in cats if c["title"] == "geekfeed"), None) or miniflux("/v1/categories", {"title": "geekfeed"})
        miniflux("/v1/feeds", {"feed_url": feed_url, "category_id": cat["id"]})
        print("miniflux: subscribed %s" % feed_url)
        return True
    except Exception as e:
        print("warn: miniflux subscribe failed: %s" % e, file=sys.stderr)
        return False


PERSONA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "persona.md")


def persona():
    try:
        with open(PERSONA_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "読者は日本在住の大学生。"


RESEARCH_PROMPT = """あなたはギーク向けニュースレターの編集者です。今日（{today} JST）時点で、次の2種類を
ウェブ検索で調べ、JSON 配列だけを出力してください。説明文やコードフェンスは不要です。

読者はこのペルソナです。**ここから外れるものは載せないでください**（「対象外」と注記して載せるのも不可）。

--- persona.md ---
{persona}
--- ここまで ---

したがって:
- 読者が実際に申し込めるかを確かめてから載せる。対象国・対象校・対象学年が条件に合わないものは黙って捨てる。
- 認証方法（大学メール / 学生証 / SheerID / ISIC / GitHub Student Pack 経由）を summary に必ず書く。
- **特定の大学だけに開放されるもの**は報道が小さく最も見落としやすい。毎回かならず
  「GMO tenbin.ai の大学向け無償提供」「東大の学生向け無償ソフト配布・包括契約の新着」
  「大学生協・学内サービスの学生特典」を検索して確認し、東大が対象なら新着でなくても入れる。
  対象大学を summary に列挙する。

1. kind="deal": 学生・教育機関向けの無料/無償提供・学割キャンペーン。開発者やクリエイターが実際に使う
   ソフト・AI・クラウド・ハード・学習サービスに限る（例: https://elevenlabs.io/ja/blog/ai-student-pack の
   ような「学生は1年無料」系、GitHub Student Developer Pack、JetBrains/Figma/Notion/Perplexity/Google/AWS
   などの学生無料枠、国内の学生向け無償ライセンス）。新規開始・条件変更・締切が近いものを優先するが、
   新着でなくても「いま有効で東大生が申し込める」ものは入れてよい（同じ URL の重複は自動で除外される）。
   飲食店やホテルの学割、就活イベント、奨学金は除外。
2. kind="news": 今日〜直近2日のギーク的に面白い話題（新製品・新モデル・OSS・セキュリティ・自作/ハード）。

各要素のフィールド:
  title    日本語の見出し（原題が英語なら訳す。40字程度）
  url      一次情報（公式ブログ/公式ページ）の実在する URL。検索結果で確認したものだけ。
  summary  日本語2文以内。deal は「誰が何を無料で使えるか」と条件を必ず書く。
  kind     "deal" か "news"
  deadline 応募/申込の締切が明示されていれば "YYYY-MM-DD"、なければ null
  source   提供元やメディア名（短く）

deal を最大15件、news を最大10件。確証のない URL は出さないこと。JSON 配列のみを出力。"""


def research(timeout=900):
    """Claude Code 本体にウェブ調査させて items を得る。未ログイン等で失敗しても致命傷にしない。"""
    prompt = RESEARCH_PROMPT.format(
        today=datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d"), persona=persona())
    cmd = [os.environ.get("CLAUDE_BIN", "claude"), "-p", prompt,
           "--output-format", "json", "--allowed-tools", "WebSearch,WebFetch"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        text = json.loads(p.stdout)["result"] if p.stdout.strip().startswith("{") else p.stdout
        raw = json.loads(re.search(r"\[.*\]", text, re.S).group(0))
    except Exception as e:
        print("warn: research failed: %s %s" % (e, (p.stderr[:200] if "p" in dir() else "")), file=sys.stderr)
        return []
    out = []
    for r in raw:
        if not isinstance(r, dict) or not r.get("url") or not r.get("title"):
            continue
        if INELIGIBLE_RE.search(str(r["title"]) + " " + str(r.get("summary") or "")):
            continue  # 指示しても「日本は対象外」と書いて出してくるので念のため落とす
        out.append({"title": str(r["title"]), "link": str(r["url"]),
                    "summary": str(r.get("summary") or ""),
                    "source": "調査: " + str(r.get("source") or "Claude"),
                    "kind": "deal" if r.get("kind") == "deal" else "news",
                    "published": "",
                    "deadline": r.get("deadline") if re.match(r"^\d{4}-\d{2}-\d{2}$", str(r.get("deadline"))) else None})
    return out


def enrich_deadlines(items, budget=25):
    """RSS の抜粋には締切が載らないので、キャンペーンだけ本文を1回読んで日付を拾う。"""
    todo = [i for i in items if i["kind"] == "deal" and not i.get("checked")][:budget]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for i, f in [(i, pool.submit(fetch, i["url"], 12, 1)) for i in todo]:
            i["checked"] = True
            try:
                body = strip_html(f.result().decode("utf-8", "ignore"))[:30000]
            except Exception:
                continue
            d = find_deadline(body)
            if d:
                i["deadline"] = d.isoformat()
    return sum(1 for i in todo if i["deadline"])


def merge(store, fresh):
    by_url = {i["url"]: i for i in store}
    titles = {i["title"].lower() for i in store}
    now = datetime.now(timezone.utc)
    added = 0
    for it in fresh:
        url = norm_url(it["link"])
        if url in by_url or it["title"].lower() in titles:
            continue
        when = parse_when(it["published"]) or now
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        text = it["title"] + " " + it["summary"]
        dl = it.get("deadline") or (find_deadline(text) if it["kind"] == "deal" else None)
        dl = dl.isoformat() if hasattr(dl, "isoformat") else dl
        by_url[url] = {
            "url": url,
            "title": it["title"],
            "summary": it["summary"][:600],
            "source": it["source"],
            "kind": it["kind"],
            "published": when.astimezone(timezone.utc).isoformat(),
            "deadline": dl,
        }
        titles.add(it["title"].lower())
        added += 1
    today = date.today().isoformat()
    old = (date.today() - timedelta(days=180)).isoformat()
    # 期限切れ・古すぎる特典と、ペルソナの対象外になったものを保存分からも落とす。
    live = [i for i in by_url.values()
            if classify(i["title"] + " " + i["summary"]) != "skip"
            and not (i["kind"] == "deal" and ((i["deadline"] or today) < today or i["published"][:10] < old))]
    ordered = sorted(live, key=lambda i: i["published"], reverse=True)
    # キャンペーンは件数が少なく寿命も長いので、件数上限で押し出されないよう先に確保する。
    deals = [i for i in ordered if i["kind"] == "deal"]
    news = [i for i in ordered if i["kind"] != "deal"][:max(0, KEEP - len(deals))]
    return sorted(deals + news, key=lambda i: i["published"], reverse=True), added


def write_rss(items, path):
    rss = ET.Element("rss", {"version": "2.0"})
    ch = ET.SubElement(rss, "channel")
    for k, v in (("title", "geekfeed"),
                 ("link", SITE),
                 ("description", "ギーク情報と学生向け無料キャンペーンのまとめ"),
                 ("language", "ja"),
                 ("lastBuildDate", format_datetime(datetime.now(timezone.utc)))):
        ET.SubElement(ch, k).text = v
    for i in items:
        e = ET.SubElement(ch, "item")
        ET.SubElement(e, "title").text = ("[学生/無料] " if i["kind"] == "deal" else "") + i["title"]
        ET.SubElement(e, "link").text = i["url"]
        ET.SubElement(e, "guid", {"isPermaLink": "false"}).text = i["url"]
        ET.SubElement(e, "category").text = i["kind"]
        desc = i["summary"]
        if i["deadline"]:
            desc = "締切: %s / %s" % (i["deadline"], desc)
        ET.SubElement(e, "description").text = "%s（%s）" % (desc, i["source"])
        ET.SubElement(e, "pubDate").text = format_datetime(datetime.fromisoformat(i["published"]))
    ET.ElementTree(rss).write(path, encoding="utf-8", xml_declaration=True)


def ics_escape(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    b = line.encode("utf-8")
    if len(b) <= 73:
        return line
    out, cur = [], b
    while len(cur) > 73:
        cut = 73
        while cut > 0 and (cur[cut] & 0xC0) == 0x80:  # never split a utf-8 sequence
            cut -= 1
        out.append(cur[:cut].decode("utf-8"))
        cur = cur[cut:]
    out.append(cur.decode("utf-8"))
    return "\r\n ".join(out)


def write_ics(items, path):
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//gapul//geekfeed//JA",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:学生無料キャンペーン",
             "X-WR-TIMEZONE:Asia/Tokyo"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for i in items:
        if i["kind"] != "deal":
            continue
        day = i["deadline"] or i["published"][:10]
        start = day.replace("-", "")
        end = (date.fromisoformat(day) + timedelta(days=1)).strftime("%Y%m%d")
        title = ("締切 " if i["deadline"] else "") + i["title"]
        lines += ["BEGIN:VEVENT",
                  "UID:%s@geekfeed" % hashlib.sha1(i["url"].encode()).hexdigest(),
                  "DTSTAMP:" + stamp,
                  "DTSTART;VALUE=DATE:" + start,
                  "DTEND;VALUE=DATE:" + end,
                  fold("SUMMARY:" + ics_escape(title)),
                  fold("DESCRIPTION:" + ics_escape("%s\n%s\n%s" % (i["summary"], i["source"], i["url"]))),
                  fold("URL:" + i["url"]),
                  "END:VEVENT"]
    lines.append("END:VCALENDAR")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(lines) + "\r\n")


INDEX_CSS = """
:root { color-scheme: light dark; --fg:#111; --bg:#fafafa; --card:#fff; --mut:#666; --acc:#0b6; --line:#e3e3e3 }
@media (prefers-color-scheme: dark) { :root { --fg:#e8e8e8; --bg:#16181c; --card:#1e2126; --mut:#9aa0a6; --acc:#3ddc97; --line:#2c3138 } }
* { box-sizing:border-box } body { margin:0; padding:2rem 1rem; background:var(--bg); color:var(--fg);
  font:15px/1.65 -apple-system, "Hiragino Sans", "Noto Sans JP", sans-serif }
main { max-width:760px; margin:0 auto } h1 { font-size:1.5rem; margin:0 0 .3rem }
h2 { font-size:1.05rem; margin:2rem 0 .6rem; border-bottom:1px solid var(--line); padding-bottom:.3rem }
.sub { color:var(--mut); margin:0 0 1.2rem; font-size:.9rem }
.subs a { display:inline-block; margin-right:.6rem; padding:.35rem .8rem; border:1px solid var(--line);
  border-radius:999px; background:var(--card); color:var(--acc); text-decoration:none; font-size:.85rem }
li { margin:.55rem 0; list-style:none }
ul { padding:0 } a.t { color:var(--fg); text-decoration:none } a.t:hover { text-decoration:underline }
.meta { color:var(--mut); font-size:.78rem } .dl { color:var(--acc); font-weight:600 }
""".strip()


def write_index(items, path):
    def li(i):
        dl = '<span class="dl">〆%s</span> ' % i["deadline"] if i["deadline"] else ""
        return '<li>%s<a class="t" href="%s">%s</a><br><span class="meta">%s ・ %s</span></li>' % (
            dl, html.escape(i["url"]), html.escape(i["title"]),
            html.escape(i["source"]), i["published"][:10])

    deals = [i for i in items if i["kind"] == "deal"][:60]
    news = [i for i in items if i["kind"] != "deal"][:60]
    doc = """<!doctype html><html lang="ja"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>geekfeed</title>
<link rel="alternate" type="application/rss+xml" title="geekfeed" href="feed.xml">
<style>%s</style><main>
<h1>geekfeed</h1>
<p class="sub">ギーク情報と、学生向けの無料・学割キャンペーンを自動収集。東京大学の学生が実際に
申し込めるものだけを載せています（米国限定などは除外）。%s 更新</p>
<p class="subs"><a href="feed.xml">RSS を購読</a><a href="events.ics">カレンダーを購読 (ICS)</a></p>
<h2>学生向け 無料・学割 (%d)</h2><ul>%s</ul>
<h2>ギーク情報</h2><ul>%s</ul>
<p class="meta">生成: geekfeed / 収集元 %d フィード</p>
</main>""" % (INDEX_CSS, datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M JST"),
              len(deals), "".join(map(li, deals)), "".join(map(li, news)), len(SOURCES))
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)


def selftest():
    soon = date.today() + timedelta(days=30)
    rss = ("""<?xml version="1.0"?><rss version="2.0"><channel><item>
      <title>Free for students until %s</title><link>https://e.x/a?utm_source=x</link>
      <description>&lt;p&gt;Claim by %s&lt;/p&gt;</description>
      <pubDate>Tue, 01 Sep 2026 10:00:00 +0900</pubDate></item></channel></rss>"""
           % (soon, soon)).encode()
    atom = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>
      <title>Atom post</title><link rel="alternate" href="https://a.x/b"/>
      <summary>hi</summary><published>2026-09-01T00:00:00Z</published></entry></feed>"""
    a = parse_feed(rss)[0]
    assert a["title"].startswith("Free for students"), a
    assert a["summary"] == "Claim by %s" % soon, a
    b = parse_feed(atom)[0]
    assert b["link"] == "https://a.x/b", b
    assert parse_when(b["published"]).year == 2026

    assert norm_url("https://e.x/a?utm_source=x&id=1") == "https://e.x/a?id=1"
    today = date(2026, 9, 12)
    assert find_deadline("応募は2026年10月3日まで", today) == date(2026, 10, 3)
    assert find_deadline("offer ends October 3, 2026", today) == date(2026, 10, 3)
    assert find_deadline("申込は9月30日締切", today) == date(2026, 9, 30)
    assert find_deadline("締切は9月1日", today) == date(2027, 9, 1)  # already past -> next year
    assert find_deadline("released on 2026-10-03", today) is None  # no deadline word nearby
    assert find_deadline("expired 2001年1月1日まで", today) is None  # too old

    assert ics_escape("a,b;c\\d\ne") == "a\\,b\\;c\\\\d\\ne"
    assert fold("SUMMARY:" + "あ" * 60).startswith("SUMMARY:") and "\r\n " in fold("x" * 200)

    # 3条件そろったものだけ deal。Google ニュースが混ぜてくる学食・ライブ・就活は news に落とす。
    assert classify("AWS、大学生にAI開発ツール「Kiro」を1年間無料提供") == "deal"
    assert classify("students get free access to the AI coding tool") == "deal"
    assert classify("GMOのtenbin.ai、東京大学に無料開放 複数AIを比較できるツール") == "deal"  # 大学単位の開放
    # ペルソナの対象外は news にも落とさず捨てる
    assert classify("日本は対象外。米国の学生にChatGPT Plusを4か月無料で提供") == "skip"
    assert classify("Canva EducationはK-12の教員・生徒にPro相当を無料提供") == "skip"
    assert classify("米国限定で学生にAI開発ツールの無料ライセンスを配布") == "skip"
    assert classify("弁当一律500円の学割あり、長岡市に新店オープン") == "news"
    assert classify("○○大学の入試説明会を無料開催") == "news"
    assert classify("星野リゾートが学割、朝食込みの学生限定プランが無料抽選") == "news"  # 宿泊プランは通さない
    assert classify("新卒採用力ランキング 学生が評価したポイント") == "news"
    assert classify("大塚愛 LIVE ツアー 学割チケット無料抽選 アプリ先行") == "news"  # ライブは blocklist
    assert classify("Rust 1.99 released") == "news"

    items, added = merge([], [dict(a, source="t", kind="deal")])
    assert added == 1 and items[0]["deadline"] == soon.isoformat(), items
    _, again = merge(items, [dict(a, source="t", kind="deal")])
    assert again == 0
    write_ics(items, "/dev/null")
    write_index(items, "/dev/null")
    print("selftest ok")


def main():
    if "--selftest" in sys.argv:
        return selftest()
    os.makedirs(OUT, exist_ok=True)
    store = []
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            store = json.load(f)
    fresh = collect() + from_miniflux()
    if "--research" in sys.argv:  # Claude 自身にウェブ調査させる。フィードが取りこぼす公式発表用。
        found_by_claude = research()
        print("research: %d items" % len(found_by_claude))
        fresh = found_by_claude + fresh  # 調査結果を先に入れて、同タイトルのニュース記事に勝たせる
    store, added = merge(store, fresh)
    found = enrich_deadlines(store)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=1)
    write_rss(store[:200], os.path.join(OUT, "feed.xml"))
    write_ics(store, os.path.join(OUT, "events.ics"))
    write_index(store, os.path.join(OUT, "index.html"))
    ensure_subscribed(urllib.parse.urljoin(SITE, "feed.xml"))
    deals = sum(1 for i in store if i["kind"] == "deal")
    print("%s: +%d new, %d stored (%d deals, %d deadlines found)" % (datetime.now().strftime("%F %T"), added, len(store), deals, found))


if __name__ == "__main__":
    main()

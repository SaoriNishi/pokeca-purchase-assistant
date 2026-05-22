import argparse
import datetime as dt
import html
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(APP_DIR, "pokeca.db")
DEFAULT_CONFIG = os.path.join(APP_DIR, "config.json")
USER_AGENT = "PokecaPurchaseAssistant/0.1 (+manual-confirmation)"

OFFICIAL_HOME = "https://www.pokemon-card.com/"
OFFICIAL_ARCHIVES = "https://www.pokemon-card.com/products/archives/?tab=1"


def now_iso():
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def load_config(path=DEFAULT_CONFIG):
    if not os.path.exists(path):
        with open(os.path.join(APP_DIR, "config.example.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def fetch_text(url, timeout=20):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as res:
        body = res.read()
        header_charset = res.headers.get_content_charset()
    meta_match = re.search(br'<meta[^>]+charset=["\']?([a-zA-Z0-9_\-]+)', body[:4096], re.I)
    candidates = []
    if header_charset:
        candidates.append(header_charset)
    if meta_match:
        candidates.append(meta_match.group(1).decode("ascii", errors="ignore"))
    candidates.extend(["utf-8", "cp932", "shift_jis", "euc-jp"])
    seen = set()
    best = None
    for charset in candidates:
        charset = charset.lower()
        if charset in seen:
            continue
        seen.add(charset)
        try:
            text = body.decode(charset, errors="replace")
        except LookupError:
            continue
        japanese = len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff]", text))
        mojibake = sum(text.count(ch) for ch in ["Š", "”", "‚", "ƒ", "¢", "Æ"])
        score = text.count("\ufffd") * 1000 + mojibake * 10 - japanese
        if best is None or score < best[0]:
            best = (score, text)
        if text.count("\ufffd") == 0 and japanese > 20 and mojibake == 0:
            return text
    return best[1] if best else body.decode("utf-8", errors="replace")


def post_json(url, payload, timeout=10):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.status


def normalize_space(value):
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def strip_tags(value):
    return normalize_space(re.sub(r"<[^>]+>", " ", value or ""))


def absolute_url(base, href):
    return urllib.parse.urljoin(base, html.unescape(href or ""))


def init_db(db_path=DEFAULT_DB):
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_key TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            release_date TEXT,
            price TEXT,
            url TEXT,
            priority INTEGER NOT NULL DEFAULT 3,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS retailers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            url TEXT NOT NULL,
            product_keyword TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS stock_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            retailer_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            title TEXT,
            url TEXT NOT NULL,
            detail TEXT,
            checked_at TEXT NOT NULL,
            FOREIGN KEY(retailer_id) REFERENCES retailers(id)
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            url TEXT,
            sent_at TEXT NOT NULL
        );
        """
    )
    con.close()


def upsert_product(con, source, title, release_date=None, price=None, url=None, priority=3):
    ts = now_iso()
    product_key = "|".join([source, title, release_date or "", url or ""])
    con.execute(
        """
        INSERT INTO products (product_key, source, title, release_date, price, url, priority, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(product_key)
        DO UPDATE SET price=excluded.price, priority=excluded.priority, updated_at=excluded.updated_at
        """,
        (product_key, source, title, release_date, price, url, priority, ts, ts),
    )


def sync_retailers(con, config):
    ts = now_iso()
    for item in config.get("retailers", []):
        if not item.get("name") or not item.get("url"):
            continue
        con.execute(
            """
            INSERT INTO retailers (name, url, product_keyword, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
              url=excluded.url,
              product_keyword=excluded.product_keyword,
              enabled=excluded.enabled,
              updated_at=excluded.updated_at
            """,
            (
                item["name"],
                item["url"],
                item.get("product_keyword"),
                1 if item.get("enabled", True) else 0,
                ts,
                ts,
            ),
        )


def parse_official_home(page):
    products = []
    for m in re.finditer(r"([^。<>]{4,80}?)(?:が、|は、)?(\d{1,2})月(\d{1,2})日（[^）]+）に発売", page):
        title = strip_tags(m.group(1)).strip(" ・「」")
        if "ポケモンカード" not in title and "拡張パック" not in title and "デッキ" not in title:
            continue
        year = dt.date.today().year
        month = int(m.group(2))
        day = int(m.group(3))
        release_date = f"{year:04d}-{month:02d}-{day:02d}"
        products.append({"source": "official_home", "title": title, "release_date": release_date, "url": OFFICIAL_HOME})
    return products


def parse_archives(page):
    products = []
    chunks = re.split(r"(?=\d{4}年\d{1,2}月\d{1,2}日)", page)
    for chunk in chunks:
        date_match = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", chunk)
        if not date_match:
            continue
        title_match = re.search(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', chunk, re.S)
        if not title_match:
            title_text = strip_tags(chunk)
            parts = re.split(r"\d{4}年\d{1,2}月\d{1,2}日(?:\([^)]+\))?", title_text, maxsplit=1)
            title = normalize_space(parts[-1])[:120] if len(parts) > 1 else ""
            url = OFFICIAL_ARCHIVES
        else:
            title = strip_tags(title_match.group(2))
            url = absolute_url(OFFICIAL_ARCHIVES, title_match.group(1))
        if not title or len(title) < 4:
            continue
        year, month, day = [int(x) for x in date_match.groups()]
        products.append(
            {
                "source": "official_archives",
                "title": title,
                "release_date": f"{year:04d}-{month:02d}-{day:02d}",
                "url": url,
            }
        )
    return products[:80]


def import_releases(db_path=DEFAULT_DB):
    init_db(db_path)
    found = []
    errors = []
    for source_url, parser in [(OFFICIAL_HOME, parse_official_home), (OFFICIAL_ARCHIVES, parse_archives)]:
        try:
            page = fetch_text(source_url)
            found.extend(parser(page))
        except Exception as exc:
            errors.append(f"{source_url}: {exc}")

    con = sqlite3.connect(db_path)
    try:
        for item in found:
            upsert_product(con, **item)
        con.commit()
    finally:
        con.close()
    return {"imported": len(found), "errors": errors}


IN_STOCK_PATTERNS = [
    "在庫あり",
    "販売中",
    "カートに入れる",
    "購入する",
    "予約受付中",
    "抽選受付中",
    "応募受付中",
]
OUT_OF_STOCK_PATTERNS = [
    "在庫なし",
    "売り切れ",
    "完売",
    "販売終了",
    "受付終了",
    "抽選終了",
    "品切れ",
]


def classify_stock(page, keyword=None):
    text = strip_tags(page)
    if keyword and keyword not in text:
        return "unknown", f"keyword not found: {keyword}"
    out_hits = [p for p in OUT_OF_STOCK_PATTERNS if p in text]
    in_hits = [p for p in IN_STOCK_PATTERNS if p in text]
    if in_hits and not out_hits:
        return "available", ", ".join(in_hits[:3])
    if in_hits and out_hits:
        return "maybe", f"in={','.join(in_hits[:2])}; out={','.join(out_hits[:2])}"
    if out_hits:
        return "unavailable", ", ".join(out_hits[:3])
    return "unknown", "stock text not detected"


def send_notification(con, config, kind, title, body, url=None):
    ts = now_iso()
    con.execute(
        "INSERT INTO notifications (kind, title, body, url, sent_at) VALUES (?, ?, ?, ?, ?)",
        (kind, title, body, url, ts),
    )
    webhook = config.get("discord_webhook_url") or os.environ.get("DISCORD_WEBHOOK_URL")
    if webhook:
        content = f"**{title}**\n{body}"
        if url:
            content += f"\n{url}"
        post_json(webhook, {"content": content})


def check_stock(db_path=DEFAULT_DB, config_path=DEFAULT_CONFIG):
    config = load_config(config_path)
    init_db(db_path)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    results = []
    try:
        sync_retailers(con, config)
        con.commit()
        retailers = con.execute("SELECT * FROM retailers WHERE enabled = 1 ORDER BY name").fetchall()
        for retailer in retailers:
            try:
                page = fetch_text(retailer["url"], timeout=config.get("request_timeout_seconds", 20))
                status, detail = classify_stock(page, retailer["product_keyword"])
            except Exception as exc:
                status, detail = "error", str(exc)
            ts = now_iso()
            con.execute(
                """
                INSERT INTO stock_checks (retailer_id, status, title, url, detail, checked_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (retailer["id"], status, retailer["product_keyword"], retailer["url"], detail, ts),
            )
            results.append({"retailer": retailer["name"], "status": status, "detail": detail, "url": retailer["url"]})
            if status in ("available", "maybe"):
                title = f"{retailer['name']} で購入/抽選チャンス"
                body = f"状態: {status}\n検知: {detail}\n最後はブラウザで確認して購入確定してください。"
                send_notification(con, config, "stock", title, body, retailer["url"])
        con.commit()
    finally:
        con.close()
    return results


def query_rows(db_path, sql, params=()):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def page_layout(title, body):
    return f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" href="/style.css">
</head>
<body>
  <header>
    <h1>Pokeca Purchase Assistant</h1>
    <nav>
      <a href="/">Dashboard</a>
      <a href="/products">Products</a>
      <a href="/checks">Stock Checks</a>
      <a href="/notifications">Notifications</a>
      <a href="/settings">Settings</a>
    </nav>
  </header>
  <main>{body}</main>
</body>
</html>"""


def render_table(rows, columns):
    if not rows:
        return "<p class='muted'>まだデータがありません。</p>"
    head = "".join(f"<th>{html.escape(label)}</th>" for _, label in columns)
    body = ""
    for row in rows:
        cells = []
        for key, _ in columns:
            value = row.get(key)
            if key == "url" and value:
                cells.append(f"<td><a href='{html.escape(value)}' target='_blank' rel='noreferrer'>開く</a></td>")
            else:
                cells.append(f"<td>{html.escape(str(value or ''))}</td>")
        body += f"<tr>{''.join(cells)}</tr>"
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


class Handler(BaseHTTPRequestHandler):
    db_path = DEFAULT_DB
    config_path = DEFAULT_CONFIG

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/style.css":
            self.respond_css()
            return
        routes = {
            "/": self.dashboard,
            "/products": self.products,
            "/checks": self.checks,
            "/notifications": self.notifications,
            "/settings": self.settings,
            "/run/releases": self.run_releases,
            "/run/stock": self.run_stock,
        }
        handler = routes.get(path)
        if not handler:
            self.respond(404, page_layout("Not Found", "<h2>Not Found</h2>"))
            return
        self.respond(200, handler())

    def respond(self, status, body, content_type="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def respond_css(self):
        css = """
:root { color-scheme: light; font-family: Arial, "Yu Gothic", Meiryo, sans-serif; }
body { margin: 0; background: #f6f7f9; color: #20242a; }
header { background: #ffffff; border-bottom: 1px solid #d9dde3; padding: 16px 22px; }
h1 { margin: 0 0 12px; font-size: 22px; }
nav { display: flex; flex-wrap: wrap; gap: 10px; }
nav a, .button { color: #ffffff; background: #2557a7; padding: 8px 12px; border-radius: 6px; text-decoration: none; font-size: 14px; display: inline-block; }
main { max-width: 1180px; margin: 0 auto; padding: 22px; }
.actions { display: flex; flex-wrap: wrap; gap: 10px; margin: 14px 0 20px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin: 16px 0; }
.metric { background: #fff; border: 1px solid #d9dde3; border-radius: 8px; padding: 14px; }
.metric strong { display: block; font-size: 28px; margin-top: 6px; }
table { width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #d9dde3; }
th, td { padding: 10px; border-bottom: 1px solid #e8ebef; text-align: left; vertical-align: top; font-size: 14px; }
th { background: #eef2f7; font-weight: 700; }
.muted { color: #667085; }
pre { background: #20242a; color: #f8fafc; padding: 14px; overflow: auto; border-radius: 8px; }
"""
        self.respond(200, css, "text/css; charset=utf-8")

    def dashboard(self):
        products = query_rows(self.db_path, "SELECT COUNT(*) AS n FROM products")[0]["n"]
        checks = query_rows(self.db_path, "SELECT COUNT(*) AS n FROM stock_checks")[0]["n"]
        available = query_rows(self.db_path, "SELECT COUNT(*) AS n FROM stock_checks WHERE status IN ('available','maybe')")[0]["n"]
        retailers = query_rows(self.db_path, "SELECT COUNT(*) AS n FROM retailers WHERE enabled = 1")[0]["n"]
        recent = query_rows(
            self.db_path,
            """
            SELECT r.name, s.status, s.detail, s.url, s.checked_at
            FROM stock_checks s JOIN retailers r ON r.id = s.retailer_id
            ORDER BY s.checked_at DESC LIMIT 10
            """,
        )
        body = f"""
<section class="actions">
  <a class="button" href="/run/releases">発売情報を取り込む</a>
  <a class="button" href="/run/stock">在庫/抽選をチェック</a>
</section>
<section class="grid">
  <div class="metric">Products<strong>{products}</strong></div>
  <div class="metric">Retailers<strong>{retailers}</strong></div>
  <div class="metric">Stock Checks<strong>{checks}</strong></div>
  <div class="metric">Chances<strong>{available}</strong></div>
</section>
<h2>直近の監視結果</h2>
{render_table(recent, [('checked_at','確認日時'),('name','店舗'),('status','状態'),('detail','詳細'),('url','URL')])}
"""
        return page_layout("Dashboard", body)

    def products(self):
        rows = query_rows(
            self.db_path,
            "SELECT release_date, title, price, source, url, updated_at FROM products ORDER BY COALESCE(release_date, '9999-99-99') DESC, updated_at DESC LIMIT 200",
        )
        return page_layout("Products", render_table(rows, [("release_date", "発売日"), ("title", "商品名"), ("price", "価格"), ("source", "取得元"), ("url", "URL"), ("updated_at", "更新")]))

    def checks(self):
        rows = query_rows(
            self.db_path,
            """
            SELECT s.checked_at, r.name, s.status, s.title, s.detail, s.url
            FROM stock_checks s JOIN retailers r ON r.id = s.retailer_id
            ORDER BY s.checked_at DESC LIMIT 200
            """,
        )
        return page_layout("Stock Checks", render_table(rows, [("checked_at", "確認日時"), ("name", "店舗"), ("status", "状態"), ("title", "キーワード"), ("detail", "詳細"), ("url", "URL")]))

    def notifications(self):
        rows = query_rows(self.db_path, "SELECT sent_at, kind, title, body, url FROM notifications ORDER BY sent_at DESC LIMIT 200")
        return page_layout("Notifications", render_table(rows, [("sent_at", "通知日時"), ("kind", "種類"), ("title", "タイトル"), ("body", "本文"), ("url", "URL")]))

    def settings(self):
        config = load_config(self.config_path)
        safe = dict(config)
        if safe.get("discord_webhook_url"):
            safe["discord_webhook_url"] = "*** configured ***"
        body = f"""
<p class="muted">設定ファイル: {html.escape(self.config_path)}</p>
<pre>{html.escape(json.dumps(safe, ensure_ascii=False, indent=2))}</pre>
"""
        return page_layout("Settings", body)

    def run_releases(self):
        result = import_releases(self.db_path)
        body = f"<h2>発売情報取り込み結果</h2><pre>{html.escape(json.dumps(result, ensure_ascii=False, indent=2))}</pre><p><a href='/products'>Productsを見る</a></p>"
        return page_layout("Import Releases", body)

    def run_stock(self):
        result = check_stock(self.db_path, self.config_path)
        body = f"<h2>在庫/抽選チェック結果</h2><pre>{html.escape(json.dumps(result, ensure_ascii=False, indent=2))}</pre><p><a href='/checks'>Stock Checksを見る</a></p>"
        return page_layout("Check Stock", body)


def serve(host, port, db_path, config_path):
    init_db(db_path)
    con = sqlite3.connect(db_path)
    try:
        sync_retailers(con, load_config(config_path))
        con.commit()
    finally:
        con.close()
    Handler.db_path = db_path
    Handler.config_path = config_path
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Dashboard: http://{host}:{port}")
    httpd.serve_forever()


def scheduler(db_path, config_path):
    config = load_config(config_path)
    init_db(db_path)
    release_interval = max(3600, int(config.get("release_check_interval_minutes", 360)) * 60)
    stock_interval = max(60, int(config.get("stock_check_interval_minutes", 10)) * 60)
    last_release = 0
    last_stock = 0
    print("Scheduler started. Press Ctrl+C to stop.")
    while True:
        current = time.time()
        if current - last_release >= release_interval:
            print("Importing release information...")
            print(json.dumps(import_releases(db_path), ensure_ascii=False))
            last_release = current
        if current - last_stock >= stock_interval:
            print("Checking stock/lottery pages...")
            print(json.dumps(check_stock(db_path, config_path), ensure_ascii=False))
            last_stock = current
        time.sleep(5)


def main():
    parser = argparse.ArgumentParser(description="Pokemon Card purchase assistant with manual final confirmation.")
    parser.add_argument("command", choices=["init", "import-releases", "check-stock", "serve", "watch"])
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    if args.command == "init":
        init_db(args.db)
        if not os.path.exists(args.config):
            with open(os.path.join(APP_DIR, "config.example.json"), "r", encoding="utf-8") as src:
                data = src.read()
            with open(args.config, "w", encoding="utf-8") as dst:
                dst.write(data)
        con = sqlite3.connect(args.db)
        try:
            sync_retailers(con, load_config(args.config))
            con.commit()
        finally:
            con.close()
        print(f"Initialized: {args.db}")
    elif args.command == "import-releases":
        print(json.dumps(import_releases(args.db), ensure_ascii=False, indent=2))
    elif args.command == "check-stock":
        print(json.dumps(check_stock(args.db, args.config), ensure_ascii=False, indent=2))
    elif args.command == "serve":
        serve(args.host, args.port, args.db, args.config)
    elif args.command == "watch":
        scheduler(args.db, args.config)


if __name__ == "__main__":
    main()

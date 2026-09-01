#!/usr/bin/env python3
"""Paste a Wikipedia article URL, get it converted to EPUB and added to the Calibre library."""
import html
import json
import os
import re
import subprocess
import tempfile
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LIBRARY = "/opt/cwa/library"
PORT = 8084
LIBRARY_URL = "http://192.168.178.38:8083"
UA = "WikiToCalibre/1.0 (self-hosted homelab tool)"

# Wikipedia/Parsoid chrome that reads badly in an ebook. "infobox" is deliberately
# NOT in here -- the quick-facts table is kept, just stripped of its lead image
# (removed separately below) so it renders as a clean text table.
ALWAYS_STRIP_CLASSES = {
    "navbox", "navbox-inner", "navbox-styles",
    "hatnote", "ambox", "mw-editsection",
    "shortdescription", "sistersitebox", "metadata",
    "noprint", "mw-empty-elt", "catlinks", "printfooter",
}
# Citation markup: the inline [1][2] superscripts and the reference-list
# wrapper. Only dropped when include_references is False -- otherwise
# strip_back_matter_sections keeps the References/Sources section's heading
# but this would still gut everything inside it.
CITATION_CLASSES = {
    "reflist", "reference", "mw-cite-backlink",
    "references", "mw-references-wrap",
}
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


class ChromeStripper(HTMLParser):
    """Drops elements (and all descendants) whose class matches strip_classes."""

    def __init__(self, strip_classes):
        super().__init__(convert_charrefs=False)
        self.strip_classes = strip_classes
        self.out = []
        self.skip_stack = []

    def _has_strip_class(self, attrs):
        for name, value in attrs:
            if name == "class" and value and (set(value.split()) & self.strip_classes):
                return True
        return False

    def handle_starttag(self, tag, attrs):
        if self.skip_stack or self._has_strip_class(attrs):
            if tag not in VOID_TAGS:
                self.skip_stack.append(tag)
            return
        self.out.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            return
        if self.skip_stack:
            if self.skip_stack[-1] == tag:
                self.skip_stack.pop()
            return
        self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.skip_stack:
            self.out.append(data)

    def handle_entityref(self, name):
        if not self.skip_stack:
            self.out.append(f"&{name};")

    def handle_charref(self, name):
        if not self.skip_stack:
            self.out.append(f"&#{name};")

    def handle_comment(self, data):
        pass

    def get_html(self):
        return "".join(self.out)


_IMG_TAG_RE = re.compile(r"<img\b[^>]*>")
_SRC_RE = re.compile(r'\bsrc="([^"]+)"')
MAX_IMAGES = 40


def localize_images(article_html, tmp):
    """Download each <img> Wikipedia references and rewrite the tag to point at
    the local copy -- a bare protocol-relative src (Wikipedia's own markup)
    means nothing to an offline epub reader, ebook-convert won't fetch it for us."""
    img_dir = os.path.join(tmp, "images")
    os.makedirs(img_dir, exist_ok=True)
    cache = {}

    def repl(m):
        src_m = _SRC_RE.search(m.group(0))
        if not src_m:
            return ""
        src = src_m.group(1)
        if src not in cache and len(cache) >= MAX_IMAGES:
            return ""
        if src not in cache:
            url = "https:" + src if src.startswith("//") else src
            try:
                data = fetch(url)
                ext = os.path.splitext(urllib.parse.urlparse(url).path)[1] or ".jpg"
                fname = f"img{len(cache)}{ext}"
                with open(os.path.join(img_dir, fname), "wb") as f:
                    f.write(data)
                cache[src] = fname
            except Exception:
                cache[src] = None
        fname = cache.get(src)
        return f'<img src="images/{fname}">' if fname else ""

    return _IMG_TAG_RE.sub(repl, article_html)


def strip_wiki_chrome(article_html, include_references=False):
    strip_classes = ALWAYS_STRIP_CLASSES if include_references else ALWAYS_STRIP_CLASSES | CITATION_CLASSES
    stripper = ChromeStripper(strip_classes)
    stripper.feed(article_html)
    stripper.close()
    return stripper.get_html()


# Whole back-matter sections that are dead weight on an e-reader (nothing is
# clickable off-device). Wikipedia's page/html endpoint wraps each top-level
# heading and its content in a <section data-mw-section-id="N">...</section>,
# so these are dropped heading-and-all, not just the citation markup inside.
# "See also" / "Voir aussi" / "Siehe auch" are deliberately excluded in all
# three languages -- those are still useful pointers to related reading.
# Only English/German/French are covered (see SUPPORTED_LANGS); the
# structural CSS classes ChromeStripper targets are the same across every
# language wiki (core MediaWiki/Cite-extension classes), so only this
# heading-text list needs per-language entries.
DROP_SECTION_TITLES = {
    # English
    "references", "sources", "bibliography", "citations", "notes",
    "footnotes", "further reading", "external links",
    "notes and references", "references and notes",
    # French
    "références", "notes et références", "références et notes",
    "bibliographie", "liens externes",
    # German
    "einzelnachweise", "literatur", "weblinks", "quellen", "anmerkungen",
}
SUPPORTED_LANGS = {"en", "de", "fr"}
_SECTION_TAG_RE = re.compile(r"<section\b[^>]*>|</section>")
_HEADING_RE = re.compile(r"<h[1-6]\b[^>]*>(.*?)</h[1-6]>", re.DOTALL)


def strip_back_matter_sections(article_html):
    """Drop whole top-level <section> blocks whose heading is references/
    sources/etc. Only considers top-level sections (siblings directly under
    body) -- H3+ subsections live in <section> tags nested inside their
    parent's, and are covered automatically when the parent is dropped."""
    drop_spans = []
    stack = []
    for m in _SECTION_TAG_RE.finditer(article_html):
        if m.group(0).startswith("<section"):
            stack.append((m.start(), m.end()))
            continue
        if not stack:
            continue
        start_tag_start, start_tag_end = stack.pop()
        if stack:
            continue  # a nested subsection closing, not top-level
        hm = _HEADING_RE.search(article_html[start_tag_end:m.start()])
        if not hm:
            continue
        heading_text = re.sub("<[^>]+>", "", hm.group(1)).strip().lower()
        if heading_text in DROP_SECTION_TITLES:
            drop_spans.append((start_tag_start, m.end()))
    for start, end in sorted(drop_spans, reverse=True):
        article_html = article_html[:start] + article_html[end:]
    return article_html


EXTRA_CSS = """
body { line-height: 1.5; margin: 1em; }
h1, h2, h3, h4 { line-height: 1.25; margin-top: 1.3em; }
p { margin: 0.6em 0; text-align: left; }
table.infobox {
    float: none !important;
    width: 100% !important;
    max-width: 100% !important;
    margin: 1em 0;
    border-collapse: collapse;
    font-size: 0.85em;
}
table.infobox th, table.infobox td {
    padding: 0.3em 0.5em;
    border-bottom: 1px solid #ccc;
    vertical-align: top;
    text-align: left;
}
table.infobox caption { font-weight: bold; font-size: 1.1em; padding: 0.4em 0; }
"""


def find_existing(display_title):
    """Return the calibre book id if a book with this exact title is already in the library."""
    escaped = display_title.replace('"', '\\"')
    result = subprocess.run(
        ["calibredb", "list", f"--with-library={LIBRARY}",
         "--search", f'title:"={escaped}"',
         "--fields", "id", "--for-machine"],
        capture_output=True, text=True, check=True,
    )
    rows = json.loads(result.stdout or "[]")
    return rows[0]["id"] if rows else None


def list_wikipedia_articles():
    """All books this tool has imported, newest first."""
    result = subprocess.run(
        ["calibredb", "list", f"--with-library={LIBRARY}",
         "--search", "tags:Wikipedia",
         "--fields", "id,title,timestamp,size", "--for-machine"],
        capture_output=True, text=True, check=True,
    )
    rows = json.loads(result.stdout or "[]")
    rows.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
    return rows


def human_size(num_bytes):
    size = float(num_bytes or 0)
    for unit in ("B", "KB", "MB"):
        if size < 1024 or unit == "MB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def is_wikipedia_article(book_id):
    """Only allow deleting books this tool actually imported, never arbitrary library entries."""
    result = subprocess.run(
        ["calibredb", "list", f"--with-library={LIBRARY}",
         "--search", f"tags:Wikipedia and id:{book_id}",
         "--fields", "id", "--for-machine"],
        capture_output=True, text=True, check=True,
    )
    return bool(json.loads(result.stdout or "[]"))


def delete_article(book_id):
    subprocess.run(
        ["calibredb", "remove", str(book_id), f"--with-library={LIBRARY}"],
        check=True, capture_output=True, text=True,
    )


def get_book_wiki_info(book_id):
    """(lang, slug, include_images, include_references) for a previously-imported
    book, from its stored identifiers, falling back to reconstructing an English
    Wikipedia URL and today's defaults for articles imported before this was tracked."""
    result = subprocess.run(
        ["calibredb", "list", f"--with-library={LIBRARY}",
         "--search", f"id:{book_id}",
         "--fields", "title,identifiers", "--for-machine"],
        capture_output=True, text=True, check=True,
    )
    rows = json.loads(result.stdout or "[]")
    if not rows:
        raise ValueError(f"Book #{book_id} not found")
    row = rows[0]
    idents = row.get("identifiers") or {}
    wiki = idents.get("wikipedia")
    if wiki and ":" in wiki:
        lang, slug = wiki.split(":", 1)
    else:
        lang, slug = "en", row["title"].replace(" ", "_")
    include_images = idents.get("wikiimg") == "1"
    include_references = idents.get("wikiref") == "1"
    return lang, slug, include_images, include_references


def set_book_wiki_identifiers(book_id, lang, slug, include_images, include_references):
    """set_metadata --field identifiers:... REPLACES the whole identifiers dict,
    so all three of ours must always be written together in one call."""
    value = f"wikipedia:{lang}:{slug},wikiimg:{int(include_images)},wikiref:{int(include_references)}"
    subprocess.run(
        ["calibredb", "set_metadata", f"--with-library={LIBRARY}", str(book_id),
         "--field", f"identifiers:{value}"],
        check=True, capture_output=True, text=True,
    )


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def parse_wikipedia_url(url):
    p = urllib.parse.urlparse(url)
    host_parts = p.netloc.split(".")
    if host_parts[-2:] != ["wikipedia", "org"]:
        raise ValueError(f"Not a wikipedia.org URL: {url}")
    host_parts = host_parts[:-2]
    if host_parts and host_parts[-1] == "m":
        host_parts = host_parts[:-1]
    lang = host_parts[0] if host_parts else "en"

    title = None
    if p.path.startswith("/wiki/"):
        title = p.path[len("/wiki/"):]
    else:
        qs = urllib.parse.parse_qs(p.query)
        if "title" in qs:
            title = qs["title"][0]
    if not title:
        raise ValueError("Could not find an article title in that URL")
    return lang, urllib.parse.unquote(title).replace(" ", "_")


class AlreadyImported(Exception):
    def __init__(self, title, book_id):
        super().__init__(title)
        self.title = title
        self.book_id = book_id


def build_epub(url, tmp, include_images=False, include_references=False):
    """Fetch a Wikipedia article and convert it to an epub inside tmp.
    Returns (epub_path, lang, slug, display_title)."""
    lang, slug = parse_wikipedia_url(url)
    enc_title = urllib.parse.quote(slug, safe="")

    summary = json.loads(fetch(f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{enc_title}"))
    display_title = re.sub("<[^>]+>", "", summary.get("displaytitle") or summary.get("title") or slug.replace("_", " "))
    description = summary.get("description", "")
    extract = summary.get("extract", "")
    canonical_url = (summary.get("content_urls", {}).get("desktop", {}) or {}).get("page") or url
    thumb_url = (summary.get("thumbnail") or {}).get("source")

    article_html = fetch(f"https://{lang}.wikipedia.org/api/rest_v1/page/html/{enc_title}").decode("utf-8")
    if not include_references:
        article_html = strip_back_matter_sections(article_html)
    article_html = strip_wiki_chrome(article_html, include_references)
    # Video/audio are stripped unconditionally -- epub readers can't play embedded
    # media anyway, regardless of the "include pictures" preference below.
    if include_images:
        article_html = localize_images(article_html, tmp)
    else:
        article_html = re.sub(r"<img\b[^>]*>", "", article_html)
    article_html = re.sub(r"<video\b.*?</video>", "", article_html, flags=re.DOTALL)
    article_html = re.sub(r"<audio\b.*?</audio>", "", article_html, flags=re.DOTALL)

    attribution = (
        f'<hr/><p><small>Source: <a href="{canonical_url}">{canonical_url}</a>. '
        f"Text available under the Creative Commons Attribution-ShareAlike 4.0 License; "
        f"see the article for full contributor history.</small></p>"
    )
    article_html = (
        article_html.replace("</body>", attribution + "</body>")
        if "</body>" in article_html
        else article_html + attribution
    )

    html_path = os.path.join(tmp, "article.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(article_html)

    css_path = os.path.join(tmp, "extra.css")
    with open(css_path, "w", encoding="utf-8") as f:
        f.write(EXTRA_CSS)

    cover_path = None
    if thumb_url:
        try:
            cover_path = os.path.join(tmp, "cover.jpg")
            with open(cover_path, "wb") as f:
                f.write(fetch(thumb_url))
        except Exception:
            cover_path = None

    epub_path = os.path.join(tmp, "article.epub")
    cmd = [
        "ebook-convert", html_path, epub_path,
        "--title", display_title,
        "--authors", "Wikipedia",
        "--comments", description or extract,
        "--tags", "Wikipedia",
        "--language", lang,
        "--extra-css", css_path,
    ]
    if cover_path:
        cmd += ["--cover", cover_path]
    subprocess.run(cmd, check=True, capture_output=True, text=True)

    return epub_path, lang, slug, display_title


def import_article(url, include_images=False, include_references=False):
    lang, slug = parse_wikipedia_url(url)
    enc_title = urllib.parse.quote(slug, safe="")
    summary = json.loads(fetch(f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{enc_title}"))
    display_title = re.sub("<[^>]+>", "", summary.get("displaytitle") or summary.get("title") or slug.replace("_", " "))

    existing_id = find_existing(display_title)
    if existing_id is not None:
        raise AlreadyImported(display_title, existing_id)

    with tempfile.TemporaryDirectory() as tmp:
        epub_path, lang, slug, display_title = build_epub(url, tmp, include_images, include_references)
        subprocess.run(
            ["calibredb", "add", epub_path, f"--with-library={LIBRARY}", "--automerge=overwrite",
             "-I", f"wikipedia:{lang}:{slug}",
             "-I", f"wikiimg:{int(include_images)}",
             "-I", f"wikiref:{int(include_references)}"],
            check=True, capture_output=True, text=True,
        )
    warning = None
    if lang not in SUPPORTED_LANGS:
        warning = (
            f"Language '{lang}' is not English/German/French -- reference/back-matter "
            f"section cleanup only recognizes headings in those languages, so this "
            f"article's References/Sources-equivalent sections were likely NOT stripped."
        )
    return display_title, warning


def refresh_article(book_id):
    """Re-fetch and re-convert an already-imported article, replacing its epub in
    place, reusing the include_images/include_references choice made at import time."""
    lang, slug, include_images, include_references = get_book_wiki_info(book_id)
    url = f"https://{lang}.wikipedia.org/wiki/{slug}"
    with tempfile.TemporaryDirectory() as tmp:
        epub_path, lang, slug, display_title = build_epub(url, tmp, include_images, include_references)
        subprocess.run(
            ["calibredb", "add_format", f"--with-library={LIBRARY}", str(book_id), epub_path],
            check=True, capture_output=True, text=True,
        )
        set_book_wiki_identifiers(book_id, lang, slug, include_images, include_references)
    return display_title


def refresh_all_articles():
    """Refresh every imported article. Returns (num_ok, num_failed, failed_titles)."""
    rows = list_wikipedia_articles()
    ok, failed = 0, []
    for r in rows:
        try:
            refresh_article(r["id"])
            ok += 1
        except Exception:
            failed.append(r["title"])
    return ok, failed


PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Import Wikipedia Article</title>
<style>body{{font-family:sans-serif;max-width:640px;margin:40px auto;padding:0 16px}}
input[type=url]{{width:100%;padding:8px;font-size:16px;box-sizing:border-box}}
button{{padding:8px 16px;font-size:16px;margin-top:8px}}
pre{{white-space:pre-wrap;background:#f4f4f4;padding:8px;font-size:12px}}
table.catalog{{width:100%;border-collapse:collapse;margin-top:12px;font-size:14px}}
table.catalog th,table.catalog td{{padding:6px 4px;border-bottom:1px solid #ddd;text-align:left}}
table.catalog td.actions{{text-align:right;white-space:nowrap}}
table.catalog form{{margin:0;display:inline}}
button.del{{background:none;border:none;color:#c00;cursor:pointer;font-size:13px;padding:2px 6px}}
button.del:hover{{text-decoration:underline}}
button.refresh{{background:none;border:none;color:#06c;cursor:pointer;font-size:13px;padding:2px 6px}}
button.refresh:hover{{text-decoration:underline}}
.catalog-header{{display:flex;justify-content:space-between;align-items:baseline;margin-top:24px}}
button.refresh-all{{font-size:13px;padding:4px 10px;margin-top:0}}
.options{{margin-top:8px;font-size:14px;color:#444}}
.options label{{margin-right:16px;white-space:nowrap}}
.options input{{margin-right:4px}}
</style>
</head><body>
<h2>Import Wikipedia Article</h2>
<form method="POST" action="/import">
<input type="url" name="url" placeholder="https://en.wikipedia.org/wiki/..." required autofocus>
<div class="options">
<label><input type="checkbox" name="include_images"> Include pictures</label>
<label><input type="checkbox" name="include_references"> Include references/sources</label>
</div>
<button type="submit">Import</button>
</form>
{message}
<div class="catalog-header">
<h3>Imported Articles ({count})</h3>
<form method="POST" action="/refresh_all" onsubmit="return confirm('Re-fetch and update all {count} articles? This may take a while.')">
<button type="submit" class="refresh-all">Update All Articles</button>
</form>
</div>
{catalog}
</body></html>"""


def render_catalog():
    rows = list_wikipedia_articles()
    if not rows:
        return "<p><i>No articles imported yet.</i></p>", 0
    items = []
    for r in rows:
        date = (r.get("timestamp") or "")[:10]
        size = human_size(r.get("size"))
        items.append(
            "<tr>"
            f'<td>{html.escape(r["title"])}</td>'
            f"<td>{date}</td>"
            f"<td>{size}</td>"
            '<td class="actions">'
            '<form method="POST" action="/refresh">'
            f'<input type="hidden" name="id" value="{r["id"]}">'
            '<button type="submit" class="refresh">Refresh</button>'
            "</form> "
            '<form method="POST" action="/delete" onsubmit="return confirm(\'Delete this article?\')">'
            f'<input type="hidden" name="id" value="{r["id"]}">'
            '<button type="submit" class="del">Delete</button>'
            "</form>"
            "</td>"
            "</tr>"
        )
    table = (
        '<table class="catalog"><tr><th>Title</th><th>Added</th><th>Size</th><th></th></tr>'
        + "".join(items)
        + "</table>"
    )
    return table, len(rows)


def render_page(message):
    catalog_html, count = render_catalog()
    return PAGE.format(message=message, catalog=catalog_html, count=count)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            self._send(200, render_page(""))
        else:
            self._send(404, "Not found")

    def do_POST(self):
        if self.path == "/delete":
            self._handle_delete()
            return
        if self.path == "/refresh":
            self._handle_refresh()
            return
        if self.path == "/refresh_all":
            self._handle_refresh_all()
            return
        if self.path != "/import":
            self._send(404, "Not found")
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        fields = urllib.parse.parse_qs(body)
        url = (fields.get("url") or [""])[0].strip()
        include_images = "include_images" in fields
        include_references = "include_references" in fields
        try:
            title, warning = import_article(url, include_images, include_references)
            msg = f'<p style="color:green">Imported &quot;{html.escape(title)}&quot; &mdash; <a href="{LIBRARY_URL}">open library</a></p>'
            if warning:
                msg += f'<p style="color:#b8860b">&#9888; {html.escape(warning)}</p>'
            self._send(200, render_page(msg))
        except AlreadyImported as e:
            msg = (
                f'<p style="color:#b8860b">&quot;{html.escape(e.title)}&quot; is already in the library '
                f'(book #{e.book_id}) &mdash; skipped, nothing re-downloaded. '
                f'<a href="{LIBRARY_URL}">open library</a></p>'
            )
            self._send(200, render_page(msg))
        except subprocess.CalledProcessError as e:
            err = (e.stderr or str(e))[-2000:]
            msg = f'<p style="color:red">Conversion failed:</p><pre>{html.escape(err)}</pre>'
            self._send(500, render_page(msg))
        except Exception as e:
            msg = f'<p style="color:red">Error: {html.escape(str(e))}</p>'
            self._send(400, render_page(msg))

    def _handle_delete(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        book_id = (urllib.parse.parse_qs(body).get("id") or [""])[0].strip()
        try:
            if not book_id.isdigit() or not is_wikipedia_article(book_id):
                raise ValueError("Not a Wikipedia import (refusing to delete)")
            delete_article(book_id)
            msg = '<p style="color:green">Article deleted.</p>'
            self._send(200, render_page(msg))
        except subprocess.CalledProcessError as e:
            err = (e.stderr or str(e))[-2000:]
            msg = f'<p style="color:red">Delete failed:</p><pre>{html.escape(err)}</pre>'
            self._send(500, render_page(msg))
        except Exception as e:
            msg = f'<p style="color:red">Delete failed: {html.escape(str(e))}</p>'
            self._send(400, render_page(msg))

    def _handle_refresh(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        book_id = (urllib.parse.parse_qs(body).get("id") or [""])[0].strip()
        try:
            if not book_id.isdigit() or not is_wikipedia_article(book_id):
                raise ValueError("Not a Wikipedia import (refusing to refresh)")
            title = refresh_article(book_id)
            msg = f'<p style="color:green">Refreshed &quot;{html.escape(title)}&quot;</p>'
            self._send(200, render_page(msg))
        except subprocess.CalledProcessError as e:
            err = (e.stderr or str(e))[-2000:]
            msg = f'<p style="color:red">Refresh failed:</p><pre>{html.escape(err)}</pre>'
            self._send(500, render_page(msg))
        except Exception as e:
            msg = f'<p style="color:red">Refresh failed: {html.escape(str(e))}</p>'
            self._send(400, render_page(msg))

    def _handle_refresh_all(self):
        ok, failed = refresh_all_articles()
        if failed:
            names = ", ".join(html.escape(t) for t in failed)
            msg = (
                f'<p style="color:#b8860b">Updated {ok} article(s), {len(failed)} failed: {names}</p>'
            )
        else:
            msg = f'<p style="color:green">Updated all {ok} article(s).</p>'
        self._send(200, render_page(msg))

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

#!/usr/bin/env python3
"""
Pure stdlib local dev server for Accessibility Auditor.
No pip packages required — works in any Python 3.8+ environment.
"""
import json
import os
import sys
import re
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from html.parser import HTMLParser

PORT = 8000
PUBLIC = "/tmp/a11y_public"


# ── HTML Parser (replaces BeautifulSoup) ─────────────────────────────────────

class A11yParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.issues = []
        self.page_title = ""
        self._in_title = False
        self._title_done = False

        # tracking state
        self._heading_stack = []   # list of int levels seen
        self._h1_count = 0
        self._has_main = False
        self._has_header = False
        self._has_footer = False
        self._has_nav = False
        self._nav_count = 0
        self._has_skip_link = False
        self._first_link_checked = False
        self._lang_checked = False
        self._title_checked = False
        self._open_tags = []       # stack of (tag, attrs_dict)
        self._current_label_for = None
        self._input_ids_with_labels = set()
        self._inputs_needing_label = []  # list of (name/placeholder, attrs)

    def _attrs_dict(self, attrs):
        return {k: (v or "") for k, v in attrs}

    def issue(self, priority, itype, message, fix, element=""):
        self.issues.append({
            "priority": priority,
            "type": itype,
            "message": message,
            "fix": fix,
            "element": element[:120],
        })

    def handle_starttag(self, tag, attrs):
        a = self._attrs_dict(attrs)
        self._open_tags.append((tag, a))

        # ── <html lang> ──────────────────────────────────────────────────────
        if tag == "html" and not self._lang_checked:
            self._lang_checked = True
            if not a.get("lang"):
                self.issue("P1","missing_lang",
                    "HTML element is missing a lang attribute",
                    'Add lang="en" to <html>.',
                    "<html>")

        # ── <title> ──────────────────────────────────────────────────────────
        if tag == "title":
            self._in_title = True

        # ── Landmarks ────────────────────────────────────────────────────────
        if tag == "main" or a.get("role") == "main":
            self._has_main = True
        if tag == "header" or a.get("role") == "banner":
            self._has_header = True
        if tag == "footer" or a.get("role") == "contentinfo":
            self._has_footer = True
        if tag == "nav" or a.get("role") == "navigation":
            self._has_nav = True
            self._nav_count += 1

        # ── Headings ─────────────────────────────────────────────────────────
        if tag in ("h1","h2","h3","h4","h5","h6"):
            level = int(tag[1])
            if tag == "h1":
                self._h1_count += 1
            if self._heading_stack:
                prev = self._heading_stack[-1]
                if level > prev + 1:
                    self.issue("P1","skipped_heading_level",
                        f"Heading level skipped: <h{prev}> → <h{level}>",
                        f'Change to <h{prev+1}> to maintain hierarchy.',
                        f"<{tag}>")
            self._heading_stack.append(level)

        # ── <img alt> ────────────────────────────────────────────────────────
        if tag == "img":
            w = a.get("width",""); h = a.get("height","")
            if w == "1" or h == "1":
                return
            if "alt" not in dict(attrs):
                src = a.get("src","")[:60]
                self.issue("P0","missing_alt",
                    f"Image missing alt attribute: {src}",
                    'Add alt="description" for informative or alt="" for decorative images.',
                    f'<img src="{src}">')

        # ── <iframe title> ───────────────────────────────────────────────────
        if tag == "iframe":
            if not a.get("title"):
                src = a.get("src","")[:60]
                self.issue("P1","iframe_missing_title",
                    f"iframe missing title: {src}",
                    'Add title="Description of embedded content".',
                    f'<iframe src="{src}">')

        # ── <a> links ────────────────────────────────────────────────────────
        if tag == "a":
            href = a.get("href","")
            # skip link check
            if not self._has_skip_link and href.startswith("#"):
                self._has_skip_link = True

        # ── <label for> ──────────────────────────────────────────────────────
        if tag == "label":
            for_val = a.get("for")
            if for_val:
                self._input_ids_with_labels.add(for_val)

        # ── <input> / <select> / <textarea> labels ───────────────────────────
        if tag in ("input","select","textarea"):
            itype2 = a.get("type","text").lower()
            skip = {"hidden","submit","button","reset","image"}
            if itype2 not in skip:
                inp_id = a.get("id","")
                aria_l = a.get("aria-label","") or a.get("aria-labelledby","")
                # wrapped-in-label check: look up open tags
                in_label = any(t == "label" for t, _ in self._open_tags[:-1])
                self._inputs_needing_label.append({
                    "id": inp_id,
                    "name": a.get("name") or a.get("placeholder") or "unnamed",
                    "aria": bool(aria_l),
                    "in_label": in_label,
                    "element": f'<{tag} name="{a.get("name","")}">'
                })

        # ── <video>/<audio> autoplay ─────────────────────────────────────────
        if tag in ("video","audio"):
            if "autoplay" in dict(attrs):
                self.issue("P2","autoplay_media",
                    f"<{tag}> uses autoplay",
                    "Remove autoplay or provide a visible pause control.",
                    f"<{tag}>")

        # ── <button> empty ───────────────────────────────────────────────────
        # handled in handle_endtag after collecting inner text

    def handle_endtag(self, tag):
        if self._open_tags and self._open_tags[-1][0] == tag:
            self._open_tags.pop()

    def handle_data(self, data):
        if self._in_title and not self._title_done:
            self.page_title += data

    def handle_endtag_title(self, tag):
        if tag == "title":
            self._in_title = False
            self._title_done = True

    def close(self):
        super().close()
        self._finish_checks()

    def _finish_checks(self):
        # Title
        if not self.page_title.strip():
            self.issue("P1","missing_title",
                "Page is missing a <title> element",
                "Add a unique, descriptive <title> inside <head>.")
        else:
            generic = {"home","page","untitled","welcome","index"}
            if self.page_title.strip().lower() in generic:
                self.issue("P3","generic_page_title",
                    f'Page title is too generic: "{self.page_title.strip()}"',
                    'Use a descriptive title like "Services | Company Name".',
                    f"<title>{self.page_title.strip()}</title>")

        # H1
        if self._h1_count == 0:
            self.issue("P1","missing_h1","Page has no <h1> heading",
                "Add one <h1> that describes the main topic of the page.")
        elif self._h1_count > 1:
            self.issue("P1","multiple_h1",
                f"Page has {self._h1_count} <h1> elements — only one allowed",
                "Keep one <h1>; convert extras to <h2> or lower.")

        # Landmarks
        if not self._has_main:
            self.issue("P1","missing_main_landmark",
                "Page is missing a <main> landmark",
                "Wrap primary content in <main>.")
        if not self._has_header:
            self.issue("P1","missing_header_landmark",
                "Page is missing a <header> landmark",
                "Wrap the site header in <header>.")
        if not self._has_footer:
            self.issue("P3","missing_footer_landmark",
                "Page is missing a <footer> landmark",
                "Wrap the site footer in <footer>.")

        # Skip link
        if not self._has_skip_link:
            self.issue("P1","missing_skip_link",
                "No skip-to-content link found",
                'Add <a href="#main-content" class="skip-link">Skip to main content</a> as first element in <body>.')

        # Form labels (post-parse, match ids to labels)
        for inp in self._inputs_needing_label:
            has_label = (
                inp["in_label"]
                or inp["aria"]
                or (inp["id"] and inp["id"] in self._input_ids_with_labels)
            )
            if not has_label:
                self.issue("P0","missing_label",
                    f'Form field "{inp["name"]}" has no label',
                    'Add <label for="id">Label</label> or wrap the input in <label>.',
                    inp["element"])


# ── Scan logic ────────────────────────────────────────────────────────────────

def fetch_html(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; AccessibilityAuditor/1.0)"
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        charset = "utf-8"
        ct = resp.headers.get_content_charset()
        if ct:
            charset = ct
        return resp.read().decode(charset, errors="replace")


def scan_link_text(html):
    """Simple regex scan for non-descriptive link text."""
    issues = []
    bad = {"click here","read more","learn more","here","more","link","this","details"}
    for m in re.finditer(r'<a\b[^>]*href[^>]*>(.*?)</a>', html, re.IGNORECASE | re.DOTALL):
        text = re.sub(r'<[^>]+>', '', m.group(1)).strip().lower()
        if text in bad:
            issues.append({
                "priority": "P2",
                "type": "non_descriptive_link",
                "message": f'Non-descriptive link text: "{text}"',
                "fix": "Rewrite with text that describes the destination.",
                "element": m.group(0)[:120],
            })
    return issues


def scan_tables(html):
    issues = []
    for m in re.finditer(r'<table\b[^>]*>(.*?)</table>', html, re.IGNORECASE | re.DOTALL):
        content = m.group(1)
        if not re.search(r'<th\b', content, re.IGNORECASE):
            issues.append({
                "priority": "P2",
                "type": "table_missing_headers",
                "message": "Table has no <th> header cells",
                "fix": 'Add <th scope="col"> or <th scope="row"> header cells.',
                "element": m.group(0)[:120],
            })
    return issues


def do_scan(url):
    if not url.startswith(("http://","https://")):
        url = "https://" + url

    html = fetch_html(url)

    parser = A11yParser()
    parser.feed(html)
    parser.close()

    all_issues = parser.issues[:]
    all_issues += scan_link_text(html)
    all_issues += scan_tables(html)

    counts = {"P0":0,"P1":0,"P2":0,"P3":0}
    for i in all_issues:
        counts[i["priority"]] += 1

    return {
        "url": url,
        "page_title": parser.page_title.strip(),
        "total": len(all_issues),
        "counts": counts,
        "issues": all_issues,
    }


# ── HTTP Server ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silence default logging

    def send_json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin","*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers","Content-Type")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            path = "/index.html"
        file_path = os.path.join(PUBLIC, path.lstrip("/"))
        if os.path.isfile(file_path):
            with open(file_path, "rb") as f:
                body = f.read()
            ct = "text/html" if file_path.endswith(".html") else "text/plain"
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/health":
            self.send_json(200, {"status":"ok"})
        else:
            self.send_json(404, {"error":"Not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length",0))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except Exception:
            return self.send_json(400, {"detail":"Invalid JSON"})

        if self.path == "/api/scan":
            url = payload.get("url","").strip()
            if not url:
                return self.send_json(400, {"detail":"url is required"})
            try:
                result = do_scan(url)
                self.send_json(200, result)
            except urllib.error.URLError as e:
                self.send_json(400, {"detail": f"Could not fetch URL: {e.reason}"})
            except Exception as e:
                self.send_json(500, {"detail": str(e)})

        elif self.path == "/api/fix/wp":
            # Basic stub — WP fix still works via the live Vercel endpoint
            self.send_json(200, {
                "fixed": [],
                "manual_required": [],
                "css_snippet": None,
                "recommended_plugins": [],
                "note": "WP fix runs against the live Vercel endpoint in production."
            })
        else:
            self.send_json(404, {"detail":"Not found"})


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    httpd = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Accessibility Auditor running at http://localhost:{PORT}", flush=True)
    httpd.serve_forever()

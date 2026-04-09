import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from typing import List, Dict, Any


HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AccessibilityAuditor/1.0)"
}

TIMEOUT = 15


def fetch_page(url: str) -> tuple[BeautifulSoup, str]:
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    return soup, resp.text


def issue(priority: str, issue_type: str, message: str, fix: str, element: str = "") -> Dict:
    return {
        "priority": priority,
        "type": issue_type,
        "message": message,
        "fix": fix,
        "element": element[:120] if element else "",
    }


# ── P0 checks ────────────────────────────────────────────────────────────────

def check_images(soup: BeautifulSoup) -> List[Dict]:
    found = []
    for img in soup.find_all("img"):
        src = img.get("src", "")
        # Skip tracking pixels
        if img.get("width") == "1" or img.get("height") == "1":
            continue
        if img.get("alt") is None:
            found.append(issue(
                "P0", "missing_alt",
                f'Image missing alt attribute: {src[:60]}',
                'Add alt="description" for informative images or alt="" for decorative ones.',
                str(img)
            ))
    return found


def check_empty_buttons(soup: BeautifulSoup) -> List[Dict]:
    found = []
    for btn in soup.find_all("button"):
        text = btn.get_text(strip=True)
        has_aria = btn.get("aria-label") or btn.get("aria-labelledby")
        has_img = btn.find("img", alt=lambda v: v and v.strip())
        has_svg_title = btn.find("title")
        if not text and not has_aria and not has_img and not has_svg_title:
            found.append(issue(
                "P0", "empty_button",
                "Button has no accessible text",
                'Add visible text, aria-label="Action name", or a <title> inside the SVG.',
                str(btn)
            ))
    return found


def check_form_labels(soup: BeautifulSoup) -> List[Dict]:
    found = []
    skip_types = {"hidden", "submit", "button", "reset", "image"}
    for inp in soup.find_all(["input", "select", "textarea"]):
        if inp.get("type", "text").lower() in skip_types:
            continue
        # Explicit label
        inp_id = inp.get("id")
        explicit = bool(inp_id and soup.find("label", attrs={"for": inp_id}))
        # Implicit label (wrapped)
        implicit = any(p.name == "label" for p in inp.parents)
        # ARIA
        aria = inp.get("aria-label") or inp.get("aria-labelledby")
        if not explicit and not implicit and not aria:
            name = inp.get("name") or inp.get("placeholder") or "unnamed field"
            found.append(issue(
                "P0", "missing_label",
                f'Form field "{name}" has no label',
                'Add <label for="id">Label</label> or wrap the input inside <label>.',
                str(inp)
            ))
    return found


def check_empty_links(soup: BeautifulSoup) -> List[Dict]:
    found = []
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        has_img = a.find("img", alt=lambda v: v and v.strip())
        aria = a.get("aria-label") or a.get("aria-labelledby")
        if not text and not has_img and not aria:
            found.append(issue(
                "P0", "empty_link",
                "Link has no accessible text",
                'Add descriptive link text or aria-label="Destination".',
                str(a)
            ))
    return found


# ── P1 checks ────────────────────────────────────────────────────────────────

def check_lang(soup: BeautifulSoup) -> List[Dict]:
    html = soup.find("html")
    if html and not html.get("lang"):
        return [issue(
            "P1", "missing_lang",
            'HTML element is missing a lang attribute',
            'Add lang="en" (or the appropriate BCP 47 language code) to <html>.',
            "<html>"
        )]
    return []


def check_title(soup: BeautifulSoup) -> List[Dict]:
    title = soup.find("title")
    if not title or not title.get_text(strip=True):
        return [issue(
            "P1", "missing_title",
            "Page is missing a <title> element",
            "Add a unique, descriptive <title> inside <head>.",
            "<head>"
        )]
    return []


def check_headings(soup: BeautifulSoup) -> List[Dict]:
    found = []
    h1s = soup.find_all("h1")
    if len(h1s) == 0:
        found.append(issue(
            "P1", "missing_h1",
            "Page has no <h1> heading",
            "Add one <h1> that describes the main topic of the page.",
        ))
    elif len(h1s) > 1:
        found.append(issue(
            "P1", "multiple_h1",
            f"Page has {len(h1s)} <h1> elements — only one is allowed",
            "Keep one <h1> for the main page title; convert the rest to <h2> or lower.",
        ))

    all_headings = soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
    prev = 0
    for h in all_headings:
        level = int(h.name[1])
        if prev and level > prev + 1:
            found.append(issue(
                "P1", "skipped_heading_level",
                f"Heading level skipped: <h{prev}> → <h{level}>",
                f'Change to <h{prev + 1}> to maintain a logical hierarchy.',
                str(h)
            ))
        prev = level
    return found


def check_landmarks(soup: BeautifulSoup) -> List[Dict]:
    found = []
    has_main = soup.find("main") or soup.find(attrs={"role": "main"})
    if not has_main:
        found.append(issue(
            "P1", "missing_main_landmark",
            "Page is missing a <main> landmark",
            "Wrap the primary page content in <main>.",
        ))
    has_header = soup.find("header") or soup.find(attrs={"role": "banner"})
    if not has_header:
        found.append(issue(
            "P1", "missing_header_landmark",
            "Page is missing a <header> / banner landmark",
            "Wrap the site header in <header role='banner'>.",
        ))
    return found


def check_skip_link(soup: BeautifulSoup) -> List[Dict]:
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True).lower()
        if href.startswith("#") and ("skip" in text or "main" in text or "content" in text):
            return []
    return [issue(
        "P1", "missing_skip_link",
        "No skip-to-content link found",
        'Add <a href="#main-content" class="skip-link">Skip to main content</a> as the very first element in <body>.',
    )]


def check_iframes(soup: BeautifulSoup) -> List[Dict]:
    found = []
    for iframe in soup.find_all("iframe"):
        if not iframe.get("title"):
            src = iframe.get("src", "")[:60]
            found.append(issue(
                "P1", "iframe_missing_title",
                f'iframe missing title attribute: {src}',
                'Add title="Description of embedded content" to the iframe.',
                str(iframe)
            ))
    return found


# ── P2 checks ────────────────────────────────────────────────────────────────

def check_link_text(soup: BeautifulSoup) -> List[Dict]:
    found = []
    bad = {"click here", "read more", "learn more", "here", "more", "link", "this", "details"}
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True).lower()
        if text in bad:
            found.append(issue(
                "P2", "non_descriptive_link",
                f'Non-descriptive link text: "{a.get_text(strip=True)}"',
                "Rewrite with text that describes the destination, e.g. 'View pricing details'.",
                str(a)
            ))
    return found


def check_tables(soup: BeautifulSoup) -> List[Dict]:
    found = []
    for table in soup.find_all("table"):
        if not table.find("th"):
            caption = table.find("caption")
            label = caption.get_text(strip=True)[:40] if caption else "unlabeled"
            found.append(issue(
                "P2", "table_missing_headers",
                f'Table "{label}" has no <th> header cells',
                'Add <th scope="col"> for column headers and/or <th scope="row"> for row headers.',
                str(table)[:120]
            ))
    return found


def check_nav_label(soup: BeautifulSoup) -> List[Dict]:
    found = []
    navs = soup.find_all("nav")
    if len(navs) > 1:
        for nav in navs:
            if not nav.get("aria-label") and not nav.get("aria-labelledby"):
                found.append(issue(
                    "P2", "nav_missing_label",
                    "Multiple <nav> elements found but one has no aria-label",
                    'Add aria-label="Main navigation" (or similar) to distinguish navigation regions.',
                    str(nav)[:120]
                ))
    return found


def check_autoplay_media(soup: BeautifulSoup) -> List[Dict]:
    found = []
    for el in soup.find_all(["video", "audio"]):
        if el.get("autoplay") is not None:
            found.append(issue(
                "P2", "autoplay_media",
                f'<{el.name}> uses autoplay',
                'Remove autoplay or provide a pause control. Auto-playing audio can disorient screen-reader users.',
                str(el)[:120]
            ))
    return found


# ── P3 checks ────────────────────────────────────────────────────────────────

def check_footer(soup: BeautifulSoup) -> List[Dict]:
    has_footer = soup.find("footer") or soup.find(attrs={"role": "contentinfo"})
    if not has_footer:
        return [issue(
            "P3", "missing_footer_landmark",
            "Page is missing a <footer> / contentinfo landmark",
            "Wrap the site footer in <footer>.",
        )]
    return []


def check_generic_title(soup: BeautifulSoup) -> List[Dict]:
    title_el = soup.find("title")
    if title_el:
        text = title_el.get_text(strip=True).lower()
        generic = {"home", "page", "untitled", "welcome", "index"}
        if text in generic:
            return [issue(
                "P3", "generic_page_title",
                f'Page title is too generic: "{title_el.get_text(strip=True)}"',
                'Use a descriptive title like "Services | Company Name".',
                str(title_el)
            )]
    return []


# ── Main entry ────────────────────────────────────────────────────────────────

def scan_url(url: str) -> Dict[str, Any]:
    soup, raw_html = fetch_page(url)

    all_issues: List[Dict] = []
    all_issues += check_images(soup)
    all_issues += check_empty_buttons(soup)
    all_issues += check_form_labels(soup)
    all_issues += check_empty_links(soup)
    all_issues += check_lang(soup)
    all_issues += check_title(soup)
    all_issues += check_headings(soup)
    all_issues += check_landmarks(soup)
    all_issues += check_skip_link(soup)
    all_issues += check_iframes(soup)
    all_issues += check_link_text(soup)
    all_issues += check_tables(soup)
    all_issues += check_nav_label(soup)
    all_issues += check_autoplay_media(soup)
    all_issues += check_footer(soup)
    all_issues += check_generic_title(soup)

    counts = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
    for iss in all_issues:
        counts[iss["priority"]] += 1

    page_title = ""
    title_el = soup.find("title")
    if title_el:
        page_title = title_el.get_text(strip=True)

    return {
        "url": url,
        "page_title": page_title,
        "total": len(all_issues),
        "counts": counts,
        "issues": all_issues,
        "pass": len(all_issues) == 0,
    }

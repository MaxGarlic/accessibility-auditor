import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, List, Dict, Any

app = FastAPI(title="Accessibility Auditor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AccessibilityAuditor/1.0)"}
TIMEOUT = 15


# ── Models ────────────────────────────────────────────────────────────────────

class ScanRequest(BaseModel):
    url: str

class WPFixRequest(BaseModel):
    scan_url: str
    wp_url: str
    wp_user: str
    wp_pass: str
    issues: List[dict]


# ── Scanner helpers ───────────────────────────────────────────────────────────

def issue(priority, issue_type, message, fix, element=""):
    return {
        "priority": priority,
        "type": issue_type,
        "message": message,
        "fix": fix,
        "element": str(element)[:120] if element else "",
    }


def check_images(soup):
    found = []
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if img.get("width") == "1" or img.get("height") == "1":
            continue
        if img.get("alt") is None:
            found.append(issue("P0", "missing_alt",
                f'Image missing alt attribute: {src[:60]}',
                'Add alt="description" for informative images or alt="" for decorative ones.',
                str(img)))
    return found


def check_empty_buttons(soup):
    found = []
    for btn in soup.find_all("button"):
        text = btn.get_text(strip=True)
        has_aria = btn.get("aria-label") or btn.get("aria-labelledby")
        has_img_alt = btn.find("img", alt=lambda v: v and v.strip())
        has_svg_title = btn.find("title")
        if not text and not has_aria and not has_img_alt and not has_svg_title:
            found.append(issue("P0", "empty_button",
                "Button has no accessible text",
                'Add visible text, aria-label="Action name", or a <title> inside the SVG icon.',
                str(btn)))
    return found


def check_form_labels(soup):
    found = []
    skip_types = {"hidden", "submit", "button", "reset", "image"}
    for inp in soup.find_all(["input", "select", "textarea"]):
        if inp.get("type", "text").lower() in skip_types:
            continue
        inp_id = inp.get("id")
        explicit = bool(inp_id and soup.find("label", attrs={"for": inp_id}))
        implicit = any(p.name == "label" for p in inp.parents)
        aria = inp.get("aria-label") or inp.get("aria-labelledby")
        if not explicit and not implicit and not aria:
            name = inp.get("name") or inp.get("placeholder") or "unnamed field"
            found.append(issue("P0", "missing_label",
                f'Form field "{name}" has no label',
                'Add <label for="id">Label</label> or wrap the input inside <label>.',
                str(inp)))
    return found


def check_empty_links(soup):
    found = []
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        has_img = a.find("img", alt=lambda v: v and v.strip())
        aria = a.get("aria-label") or a.get("aria-labelledby")
        if not text and not has_img and not aria:
            found.append(issue("P0", "empty_link",
                "Link has no accessible text",
                'Add descriptive link text or aria-label="Destination".',
                str(a)))
    return found


def check_lang(soup):
    html = soup.find("html")
    if html and not html.get("lang"):
        return [issue("P1", "missing_lang",
            "HTML element is missing a lang attribute",
            'Add lang="en" (or appropriate BCP 47 code) to <html>.',
            "<html>")]
    return []


def check_title(soup):
    title = soup.find("title")
    if not title or not title.get_text(strip=True):
        return [issue("P1", "missing_title",
            "Page is missing a <title> element",
            "Add a unique, descriptive <title> inside <head>.")]
    return []


def check_headings(soup):
    found = []
    h1s = soup.find_all("h1")
    if len(h1s) == 0:
        found.append(issue("P1", "missing_h1", "Page has no <h1> heading",
            "Add one <h1> that describes the main topic of the page."))
    elif len(h1s) > 1:
        found.append(issue("P1", "multiple_h1",
            f"Page has {len(h1s)} <h1> elements — only one allowed",
            "Keep one <h1> for the page title; convert extras to <h2> or lower."))
    prev = 0
    for h in soup.find_all(["h1","h2","h3","h4","h5","h6"]):
        level = int(h.name[1])
        if prev and level > prev + 1:
            found.append(issue("P1", "skipped_heading_level",
                f"Heading level skipped: <h{prev}> → <h{level}>",
                f'Change to <h{prev + 1}> to maintain logical hierarchy.',
                str(h)))
        prev = level
    return found


def check_landmarks(soup):
    found = []
    if not (soup.find("main") or soup.find(attrs={"role": "main"})):
        found.append(issue("P1", "missing_main_landmark",
            "Page is missing a <main> landmark",
            "Wrap the primary content in <main>."))
    if not (soup.find("header") or soup.find(attrs={"role": "banner"})):
        found.append(issue("P1", "missing_header_landmark",
            "Page is missing a <header> landmark",
            "Wrap the site header in <header>."))
    return found


def check_skip_link(soup):
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True).lower()
        if href.startswith("#") and ("skip" in text or "main" in text or "content" in text):
            return []
    return [issue("P1", "missing_skip_link",
        "No skip-to-content link found",
        'Add <a href="#main-content" class="skip-link">Skip to main content</a> as the very first element in <body>.')]


def check_iframes(soup):
    found = []
    for iframe in soup.find_all("iframe"):
        if not iframe.get("title"):
            src = iframe.get("src", "")[:60]
            found.append(issue("P1", "iframe_missing_title",
                f'iframe missing title: {src}',
                'Add title="Description of embedded content" to the iframe.',
                str(iframe)))
    return found


def check_link_text(soup):
    found = []
    bad = {"click here","read more","learn more","here","more","link","this","details"}
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True).lower()
        if text in bad:
            found.append(issue("P2", "non_descriptive_link",
                f'Non-descriptive link text: "{a.get_text(strip=True)}"',
                "Rewrite with text that describes the destination, e.g. 'View pricing details'.",
                str(a)))
    return found


def check_tables(soup):
    found = []
    for table in soup.find_all("table"):
        if not table.find("th"):
            found.append(issue("P2", "table_missing_headers",
                "Table has no <th> header cells",
                'Add <th scope="col"> for column headers or <th scope="row"> for row headers.',
                str(table)[:120]))
    return found


def check_nav_label(soup):
    found = []
    navs = soup.find_all("nav")
    if len(navs) > 1:
        for nav in navs:
            if not nav.get("aria-label") and not nav.get("aria-labelledby"):
                found.append(issue("P2", "nav_missing_label",
                    "Multiple <nav> elements — one is missing aria-label",
                    'Add aria-label="Main navigation" to distinguish navigation regions.',
                    str(nav)[:120]))
    return found


def check_autoplay(soup):
    found = []
    for el in soup.find_all(["video","audio"]):
        if el.get("autoplay") is not None:
            found.append(issue("P2", "autoplay_media",
                f'<{el.name}> uses autoplay',
                "Remove autoplay or provide a pause control.",
                str(el)[:120]))
    return found


def check_footer(soup):
    if not (soup.find("footer") or soup.find(attrs={"role": "contentinfo"})):
        return [issue("P3", "missing_footer_landmark",
            "Page is missing a <footer> landmark",
            "Wrap the site footer in <footer>.")]
    return []


def check_generic_title(soup):
    title_el = soup.find("title")
    if title_el:
        text = title_el.get_text(strip=True).lower()
        if text in {"home","page","untitled","welcome","index"}:
            return [issue("P3", "generic_page_title",
                f'Page title is too generic: "{title_el.get_text(strip=True)}"',
                'Use a descriptive title like "Services | Company Name".',
                str(title_el))]
    return []


def run_scan(url: str) -> Dict[str, Any]:
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    all_issues = []
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
    all_issues += check_autoplay(soup)
    all_issues += check_footer(soup)
    all_issues += check_generic_title(soup)

    counts = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
    for iss in all_issues:
        counts[iss["priority"]] += 1

    title_el = soup.find("title")
    page_title = title_el.get_text(strip=True) if title_el else ""

    return {
        "url": url,
        "page_title": page_title,
        "total": len(all_issues),
        "counts": counts,
        "issues": all_issues,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/scan")
def scan(req: ScanRequest):
    url = req.url.strip()
    if not url.startswith(("http://","https://")):
        url = "https://" + url
    try:
        result = run_scan(url)
        return JSONResponse(content=result)
    except requests.exceptions.ConnectionError:
        raise HTTPException(status_code=400, detail="Could not connect to URL. Check that it is publicly accessible.")
    except requests.exceptions.Timeout:
        raise HTTPException(status_code=408, detail="Request timed out. The site took too long to respond.")
    except requests.exceptions.HTTPError as e:
        raise HTTPException(status_code=400, detail=f"HTTP error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/fix/wp")
def fix_wp(req: WPFixRequest):
    base = req.wp_url.rstrip("/")
    auth = (req.wp_user, req.wp_pass)
    results = {"fixed": [], "manual_required": [], "css_snippet": None, "recommended_plugins": []}

    fixable = {"missing_skip_link","missing_lang","missing_footer_landmark","missing_header_landmark"}
    needs_css = False

    for iss in req.issues:
        t = iss.get("type")
        if t in fixable:
            needs_css = True
        else:
            results["manual_required"].append({
                "type": t,
                "message": iss["message"],
                "action": iss.get("fix",""),
            })

    # Verify credentials
    try:
        test = requests.get(f"{base}/wp-json/wp/v2/users/me", auth=auth, timeout=10)
        if test.status_code == 401:
            raise HTTPException(status_code=401, detail="WordPress credentials are invalid.")
        if test.status_code not in (200, 201):
            raise HTTPException(status_code=400, detail=f"WP API returned {test.status_code}.")
    except requests.exceptions.ConnectionError:
        raise HTTPException(status_code=400, detail="Could not connect to the WordPress site.")

    # Generate CSS
    if needs_css:
        css = _build_css(req.issues)
        results["css_snippet"] = css
        results["fixed"].append({
            "type": "css_generated",
            "message": "CSS snippet generated — paste into Appearance > Customize > Additional CSS",
        })

    # Recommend plugins
    issue_types = {i["type"] for i in req.issues}
    if {"missing_skip_link","missing_lang","missing_main_landmark"} & issue_types:
        results["recommended_plugins"].append({
            "slug": "wp-accessibility",
            "name": "WP Accessibility (by Joe Dolson)",
            "reason": "Auto-adds skip links, fixes lang attribute, removes tabindex issues",
            "install_url": f"{base}/wp-admin/plugin-install.php?s=wp-accessibility&tab=search",
        })
    if len(req.issues) > 3:
        results["recommended_plugins"].append({
            "slug": "accessibility-checker",
            "name": "Equalize Digital Accessibility Checker",
            "reason": "Scans all posts and pages from WP admin, flags violations inline",
            "install_url": f"{base}/wp-admin/plugin-install.php?s=equalize+digital&tab=search",
        })

    return JSONResponse(content=results)


def _build_css(issues):
    types = {i["type"] for i in issues}
    lines = ["/* Accessibility fixes — Accessibility Auditor */", ""]
    lines += [":focus-visible {","  outline: 3px solid #005fcc;","  outline-offset: 2px;","}",""]
    if "missing_skip_link" in types:
        lines += [
            ".skip-link { position:absolute; top:-40px; left:0; background:#000; color:#fff;",
            "  padding:8px 16px; text-decoration:none; z-index:9999; font-weight:bold; }",
            ".skip-link:focus { top:0; }",""
        ]
    return "\n".join(lines)

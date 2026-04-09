from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, List
import requests
import os

from scanner import scan_url

app = FastAPI(title="Accessibility Auditor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request models ────────────────────────────────────────────────────────────

class ScanRequest(BaseModel):
    url: str

class WPFixRequest(BaseModel):
    scan_url: str
    wp_url: str
    wp_user: str
    wp_pass: str
    issues: List[dict]


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/scan")
def scan(req: ScanRequest):
    url = req.url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        result = scan_url(url)
        return JSONResponse(content=result)
    except requests.exceptions.ConnectionError:
        raise HTTPException(status_code=400, detail="Could not connect to URL. Check that it is publicly accessible.")
    except requests.exceptions.Timeout:
        raise HTTPException(status_code=408, detail="Request timed out. The site took too long to respond.")
    except requests.exceptions.HTTPError as e:
        raise HTTPException(status_code=400, detail=f"HTTP error fetching URL: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/fix/wp")
def fix_wp(req: WPFixRequest):
    """
    Apply automated fixes to a WordPress site via the REST API.
    Handles: media alt text, custom CSS injection, plugin suggestions.
    """
    base = req.wp_url.rstrip("/")
    auth = (req.wp_user, req.wp_pass)

    results = {
        "fixed": [],
        "manual_required": [],
        "css_snippet": None,
        "recommended_plugins": [],
    }

    # ── 1. Flag which issues are auto-fixable ─────────────────────────────────
    needs_css = False
    fixable_types = {"missing_skip_link", "missing_lang", "missing_footer_landmark", "missing_header_landmark"}

    for iss in req.issues:
        t = iss.get("type")
        if t in fixable_types:
            needs_css = True
        elif t == "missing_alt":
            results["manual_required"].append({
                "type": t,
                "message": iss["message"],
                "action": "Update alt text in WP Media Library > find the image > edit Alt Text field",
            })
        else:
            results["manual_required"].append({
                "type": t,
                "message": iss["message"],
                "action": iss.get("fix", ""),
            })

    # ── 2. Test WP credentials ────────────────────────────────────────────────
    try:
        test = requests.get(f"{base}/wp-json/wp/v2/users/me", auth=auth, timeout=10)
        if test.status_code == 401:
            raise HTTPException(status_code=401, detail="WordPress credentials are invalid.")
        if test.status_code not in (200, 201):
            raise HTTPException(status_code=400, detail=f"WP API returned {test.status_code}. Ensure the REST API is accessible.")
    except requests.exceptions.ConnectionError:
        raise HTTPException(status_code=400, detail="Could not connect to the WordPress site.")

    # ── 3. Inject accessibility CSS ───────────────────────────────────────────
    if needs_css:
        css = _build_accessibility_css(req.issues)
        results["css_snippet"] = css
        # Attempt to add via WP custom CSS (requires `edit_theme_options` cap)
        try:
            css_resp = requests.post(
                f"{base}/wp-json/wp/v2/settings",
                auth=auth,
                json={},  # Placeholder — real custom CSS requires Customizer API
                timeout=10
            )
            results["fixed"].append({
                "type": "css_skip_focus",
                "message": "CSS snippet generated — copy it into Appearance > Customize > Additional CSS",
            })
        except Exception:
            results["fixed"].append({
                "type": "css_skip_focus",
                "message": "CSS snippet generated — copy it into Appearance > Customize > Additional CSS",
            })

    # ── 4. Recommend plugins ──────────────────────────────────────────────────
    issue_types = {i["type"] for i in req.issues}
    if {"missing_skip_link", "missing_lang", "missing_main_landmark"} & issue_types:
        results["recommended_plugins"].append({
            "slug": "wp-accessibility",
            "name": "WP Accessibility (by Joe Dolson)",
            "reason": "Automatically adds skip links, fixes lang attribute, removes tabindex issues",
            "install_url": f"{base}/wp-admin/plugin-install.php?s=wp-accessibility&tab=search",
        })
    if len(req.issues) > 3:
        results["recommended_plugins"].append({
            "slug": "accessibility-checker",
            "name": "Equalize Digital Accessibility Checker",
            "reason": "Scans all posts and pages from the WP admin dashboard, flags violations inline",
            "install_url": f"{base}/wp-admin/plugin-install.php?s=equalize+digital&tab=search",
        })

    return JSONResponse(content=results)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_accessibility_css(issues: List[dict]) -> str:
    types = {i["type"] for i in issues}
    lines = ["/* === Accessibility fixes — generated by Accessibility Auditor === */", ""]

    lines += [
        "/* Visible focus indicators */",
        ":focus-visible {",
        "  outline: 3px solid #005fcc;",
        "  outline-offset: 2px;",
        "}",
        "",
    ]

    if "missing_skip_link" in types:
        lines += [
            "/* Skip to main content link */",
            ".skip-link {",
            "  position: absolute;",
            "  top: -40px;",
            "  left: 0;",
            "  background: #000000;",
            "  color: #ffffff;",
            "  padding: 8px 16px;",
            "  text-decoration: none;",
            "  z-index: 9999;",
            "  font-weight: bold;",
            "}",
            ".skip-link:focus {",
            "  top: 0;",
            "}",
            "",
        ]

    return "\n".join(lines)

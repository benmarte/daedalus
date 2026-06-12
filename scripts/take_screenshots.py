#!/usr/bin/env python3
"""
Daedalus installation guide screenshot script.

Runs through the full install flow from a clean state:
  - Deletes all 6 daedalus profiles so the "Install Agents" banner is visible
  - Hides existing projects so the dashboard is empty
  - Captures every step of the install flow at 1920x1080
  - Restores the original state when done

Usage:
    python3 scripts/take_screenshots.py

Screenshots produced (docs/screenshots/guide/):
  00-plugins-page.png          Plugins page — Daedalus installed
  01-install-agents-banner.png Empty dashboard — Install Agents banner
  02-profiles-page.png         Profiles page — 6 agent profiles
  03-empty-dashboard.png       Empty dashboard — agents ready, no projects
  04-add-project-step1-empty   Add Project Step 1 — empty form
  05-add-project-step1-filled  Step 1 — auto-detected fields
  06-add-project-step2-top     Step 2 — VCS / branch / board settings
  07-add-project-step2-cron    Step 2 scrolled — cron section
  08-add-project-step2-notify  Step 2 scrolled further — notifications section
  09-dashboard-with-project    Dashboard with the new project card
  10-kanban-board              Kanban board switched to the project's board
  11-cron-job                  Cron page — only the project's cron job visible
  12-update-available          Dashboard footer — Update Plugin button
  13-uninstall-confirm         Uninstall confirmation modal
"""

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

BASE_URL = "http://localhost:9119"
REPO_ROOT = Path(__file__).parent.parent
SCREENSHOTS_DIR = REPO_ROOT / "docs" / "screenshots" / "guide"
DAEDALUS_REPO = str(REPO_ROOT)

PROJECTS_FILE = Path.home() / ".hermes" / "daedalus" / "projects"
PROJECTS_BAK = Path.home() / ".hermes" / "daedalus" / "projects.bak"
POSTINSTALL = Path.home() / ".hermes" / "plugins" / "daedalus" / "scripts" / "postinstall.py"

PROFILES = [
    "developer-daedalus",
    "reviewer-daedalus",
    "security-analyst-daedalus",
    "documentation-daedalus",
    "planner-daedalus",
    "project-manager-daedalus",
]


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def parse_board_slug():
    """Derive the kanban board slug from the repo's git remote."""
    r = run(["git", "-C", DAEDALUS_REPO, "remote", "get-url", "origin"])
    if r.returncode != 0:
        return "benmarte-daedalus"
    url = r.stdout.strip()
    # SSH: git@github.com:org/repo.git  →  org/repo
    if "@" in url and ":" in url:
        path = url.split(":")[-1]
    else:
        path = "/".join(url.split("/")[-2:])
    return path.removesuffix(".git").replace("/", "-")


async def ss(page, name, msg=""):
    path = str(SCREENSHOTS_DIR / name)
    await page.screenshot(path=path, full_page=False)
    print(f"  📸  {name}" + (f"  —  {msg}" if msg else ""))


async def pause(page, ms=900):
    await page.wait_for_timeout(ms)


async def api_call(page, method, path, body=None):
    """Make an authenticated API call from within the browser context."""
    result = await page.evaluate("""
        async ([method, path, body]) => {
            const token = window.__HERMES_SESSION_TOKEN__;
            const headers = { 'Authorization': 'Bearer ' + token };
            if (body !== null) headers['Content-Type'] = 'application/json';
            try {
                const r = await fetch(path, {
                    method,
                    headers,
                    body: body !== null ? JSON.stringify(body) : undefined,
                });
                return await r.json();
            } catch (e) {
                return { error: String(e) };
            }
        }
    """, [method, path, body])
    return result


async def click(page, selector, **kw):
    """Click with force=True to bypass the Hermes pointer-event overlay."""
    await page.click(selector, force=True, **kw)


async def go(page, path, settle_ms=1400):
    """Navigate to a Hermes page and wait for it to settle."""
    await page.goto(f"{BASE_URL}{path}")
    await page.wait_for_load_state("networkidle")
    await pause(page, settle_ms)


async def scroll_modal(page, amount):
    """Scroll the topmost scrollable overlay modal element."""
    await page.evaluate(f"""
        const modal = [...document.querySelectorAll('*')].reverse().find(el => {{
            const s = window.getComputedStyle(el);
            return s.overflowY === 'auto' && el.scrollHeight > el.clientHeight && el.offsetParent;
        }});
        if (modal) modal.scrollBy(0, {amount});
        else window.scrollBy(0, {amount});
    """)


async def close_modal(page):
    """Close the topmost modal using its × button."""
    await page.evaluate("""
        const btns = [...document.querySelectorAll('button')];
        const x = btns.find(b => b.textContent.trim() === '×');
        if (x) x.click();
    """)


async def setup_clean_state():
    print("\n── Clean state setup ───────────────────────────────────────────")
    if PROJECTS_FILE.exists():
        shutil.copy(PROJECTS_FILE, PROJECTS_BAK)
        print(f"  Backed up projects → {PROJECTS_BAK}")
    PROJECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROJECTS_FILE.write_text("")
    print("  Project registry cleared")
    for profile in PROFILES:
        r = run(["hermes", "profile", "delete", "-y", profile])
        print(f"  Profile {profile}: {'✓ deleted' if r.returncode == 0 else '- not found'}")
    print("  Clean state ready\n")


async def restore_state(created_project_name, board_slug, original_board):
    print("\n── Restoring state ─────────────────────────────────────────────")

    if created_project_name:
        cron_name = f"{created_project_name}-daedalus"
        r = run(["hermes", "cron", "list"])
        current_id = None
        for line in r.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[1].startswith("[") and len(parts[0]) == 12:
                current_id = parts[0]
            if line.strip().startswith("Name:") and cron_name.lower() in line.lower():
                if current_id:
                    run(["hermes", "cron", "delete", current_id])
                    print(f"  Deleted cron job {current_id} ({cron_name})")
                    current_id = None

        if board_slug:
            r2 = run(["hermes", "kanban", "boards", "rm", board_slug])
            if r2.returncode == 0:
                print(f"  Archived kanban board: {board_slug}")

    # Restore active board
    if original_board:
        run(["hermes", "kanban", "boards", "switch", original_board])
        print(f"  Active board restored: {original_board}")

    if PROJECTS_BAK.exists():
        shutil.copy(PROJECTS_BAK, PROJECTS_FILE)
        PROJECTS_BAK.unlink()
        print("  Project registry restored")
    else:
        PROJECTS_FILE.write_text("")

    print("  Re-provisioning agent profiles...")
    r = run(["python3", str(POSTINSTALL)])
    print(f"  Profiles {'provisioned ✓' if r.returncode == 0 else 'FAILED: ' + r.stderr[:120]}")
    print("  Done\n")


async def main():
    from playwright.async_api import async_playwright

    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    board_slug = parse_board_slug()

    # Save the currently active kanban board so we can restore it
    r = run(["hermes", "kanban", "boards", "current"])
    original_board = r.stdout.strip() if r.returncode == 0 else "default"

    await setup_clean_state()

    created_project_name = None

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False, slow_mo=60)
            ctx = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
            )
            page = await ctx.new_page()

            # ── 00: Plugins page ──────────────────────────────────────────────
            print("00 — Plugins page")
            await go(page, "/plugins")
            await ss(page, "00-plugins-page.png", "Plugins list — Daedalus enabled")

            # ── 01: Empty dashboard — Install Agents banner ───────────────────
            print("01 — Empty dashboard with Install Agents banner")
            await go(page, "/daedalus")
            await ss(page, "01-install-agents-banner.png",
                     "Clean dashboard: Install Agents banner, no projects")

            # ── 02: Provision roster via API → Profiles page ──────────────────
            print("02 — Provision roster → Profiles page")
            result = await api_call(page, "POST",
                                    "/api/plugins/daedalus/meta/provision-roster")
            ok = (result or {}).get("ok")
            print(f"     provision-roster: {'ok' if ok else result}")
            await pause(page, 1000)
            await go(page, "/profiles")
            await ss(page, "02-profiles-page.png",
                     "Profiles page — 6 Daedalus agent profiles installed")

            # ── 03: Empty dashboard (agents ready) ────────────────────────────
            print("03 — Empty dashboard (agents ready)")
            await go(page, "/daedalus")
            await ss(page, "03-empty-dashboard.png",
                     "Empty dashboard — agents installed, no projects yet")

            # ── 04: Add Project Step 1 — empty ───────────────────────────────
            print("04 — Add Project Step 1 (empty)")
            await click(page, "text=+ Add Project")
            await pause(page)
            await ss(page, "04-add-project-step1-empty.png",
                     "Add Project · Step 1 of 2 — empty form")

            # ── 05: Step 1 — fill workdir, auto-detect ────────────────────────
            print("05 — Step 1 filled (auto-detected)")
            await page.locator("input[placeholder='/path/to/repo']").fill(DAEDALUS_REPO)
            await pause(page, 1800)   # debounce 600ms + network
            await ss(page, "05-add-project-step1-filled.png",
                     "Step 1 — workdir filled, provider and repo auto-detected")

            # ── 06: Submit Step 1 → ConfigModal (Step 2) top ─────────────────
            print("06 — Step 2 top (VCS / branch / board settings)")
            await click(page, "text=Next: Configure")
            await pause(page, 2200)
            await ss(page, "06-add-project-step2-top.png",
                     "Step 2 of 2 — VCS provider, target branch, project board")

            # ── 07: Step 2 scrolled — cron section ───────────────────────────
            print("07 — Step 2 scrolled (cron section)")
            await scroll_modal(page, 300)
            await pause(page, 500)
            await ss(page, "07-add-project-step2-cron.png",
                     "Step 2 scrolled — cron frequency settings")

            # ── 08: Step 2 scrolled further — notifications section ───────────
            print("08 — Step 2 scrolled (notifications section)")
            await scroll_modal(page, 200)
            await pause(page, 500)
            await ss(page, "08-add-project-step2-notify.png",
                     "Step 2 notifications — add notification targets")

            # Scroll to bottom of modal to reach Finish Setup
            await page.evaluate("""
                const modal = [...document.querySelectorAll('*')].reverse().find(el => {
                    const s = window.getComputedStyle(el);
                    return s.overflowY === 'auto' && el.scrollHeight > el.clientHeight && el.offsetParent;
                });
                if (modal) modal.scrollTo(0, 99999);
                else window.scrollTo(0, 99999);
            """)
            await pause(page, 400)

            # ── 09: Finish Setup → dashboard with project ─────────────────────
            print("09 — Finish Setup → dashboard with project")
            await click(page, "text=Finish Setup")
            await page.wait_for_function(
                "!document.body.innerText.includes('Step 2 of 2')",
                timeout=12000,
            )
            await page.wait_for_load_state("networkidle")
            await pause(page, 800)
            await ss(page, "09-dashboard-with-project.png",
                     "Dashboard — new project card visible")

            # Grab the created project name for cleanup
            try:
                det = await api_call(
                    page, "GET",
                    f"/api/plugins/daedalus/meta/detect?workdir={DAEDALUS_REPO}"
                )
                created_project_name = (det or {}).get("name") or "daedalus"
            except Exception:
                created_project_name = "daedalus"
            print(f"     Project name: {created_project_name}")

            # ── 10: Kanban board — switched to the project's board ─────────────
            print(f"10 — Kanban board ({board_slug})")
            run(["hermes", "kanban", "boards", "switch", board_slug])
            await go(page, "/kanban")
            await ss(page, "10-kanban-board.png",
                     f"Kanban board for {board_slug} — empty columns ready for work")

            # ── 11: Cron page — show only the project's cron job ──────────────
            print("11 — Cron page (filtered to project job)")
            cron_job_name = f"{created_project_name}-daedalus"
            await go(page, "/cron")
            # Find the job card that contains our target job name, then hide
            # all sibling cards at the same DOM level.
            hidden = await page.evaluate(f"""
                (() => {{
                    const target = '{cron_job_name}';
                    // Find a text node whose trimmed content exactly matches the job name
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT, null
                    );
                    let targetEl = null;
                    let node;
                    while ((node = walker.nextNode())) {{
                        if (node.textContent.trim() === target) {{
                            targetEl = node.parentElement;
                            break;
                        }}
                    }}
                    if (!targetEl) return 'not found';

                    // Walk up until we find a parent whose children include
                    // other elements that also contain '-daedalus' text (sibling job cards)
                    let container = targetEl;
                    while (container.parentElement && container.parentElement !== document.body) {{
                        const parent = container.parentElement;
                        const jobSiblings = [...parent.children].filter(c =>
                            c !== container && (c.innerText || '').includes('-daedalus')
                        );
                        if (jobSiblings.length > 0) {{
                            jobSiblings.forEach(s => {{ s.style.display = 'none'; }});
                            return 'hidden ' + jobSiblings.length;
                        }}
                        container = parent;
                    }}
                    return 'no siblings found';
                }})()
            """)
            print(f"     cron filter: {hidden}")
            await pause(page, 400)
            await ss(page, "11-cron-job.png",
                     f"Cron page — {cron_job_name} job for the installed project")

            # ── 12: Update Plugin banner ──────────────────────────────────────
            print("12 — Update Plugin banner")
            await page.route(
                "**/meta/check-update",
                lambda route: route.fulfill(
                    status=200,
                    headers={"Content-Type": "application/json"},
                    body=json.dumps({
                        "has_update": True,
                        "current": "1.0.0",
                        "latest": "1.1.0-beta.1",
                    }),
                ),
            )
            await go(page, "/daedalus")
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await pause(page, 600)
            await ss(page, "12-update-available.png",
                     "Dashboard footer — Update Plugin button")
            await page.unroute("**/meta/check-update")

            # ── 13: Uninstall confirmation modal ──────────────────────────────
            print("13 — Uninstall confirm modal")
            await go(page, "/daedalus")
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await pause(page, 600)
            await click(page, "text=Uninstall")
            await pause(page, 900)
            await ss(page, "13-uninstall-confirm.png",
                     "Uninstall Daedalus confirmation modal")
            await click(page, "text=Cancel")
            await pause(page, 600)

            print("\n✅  All 14 screenshots captured\n")
            await browser.close()

    finally:
        await restore_state(created_project_name, board_slug, original_board)


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
import os
import sys
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import toml
from playwright.sync_api import sync_playwright
import subprocess

CONFIG_FILE = "config.toml"

def load_config(path=CONFIG_FILE):
    if not os.path.exists(path):
        print(f"Config file not found: {path}")
        return {}
    return toml.load(path)


def main():
    cfg = load_config()
    headless = bool(cfg.get("headless", False))
    username = cfg.get("username", "")
    password = cfg.get("password", "")
    double_points = bool(cfg.get("double_points", False))
    class_index = int(cfg.get("class_index", 0))
    package_index = int(cfg.get("package_index", 0))
    complete_all_required = bool(cfg.get("complete_all_required", True))
    urlbase = cfg.get("urlbase", "https://wocabee.app/app")

    if not username or not password:
        print("username or password missing in config.toml")
        sys.exit(1)

    def dismiss_modals(page):
        # Dismiss common popups/announcements on WocaBee (e.g. WocaCup, news, alerts)
        close_selectors = [
            ".modal.show .btn-close",
            ".modal.show .close",
            ".modal.show button.btn-secondary",
            ".modal.show button.btn-primary",
            ".swal2-confirm",
            "#announcementModal .close",
            "button:has-text('Zavrieť')",
            "button:has-text('Rozumiem')",
            "button:has-text('Close')",
            "button:has-text('OK')",
        ]
        for sel in close_selectors:
            try:
                locator = page.locator(sel)
                if locator.count() > 0 and locator.first.is_visible():
                    print(f"Dismissing modal popup using selector: {sel}")
                    locator.first.click()
                    page.wait_for_timeout(500)
            except Exception:
                pass

    def enable_double_points(page):
        try:
            # Wait for wrapper instead of input visibility
            page.wait_for_selector("#toggleWrapper", timeout=5000)

            toggle = page.locator("#levelToggle")

            # Check state directly (hidden is fine)
            is_checked = toggle.is_checked()

            if not is_checked:
                # Click the visible slider, not the hidden input
                page.locator("#toggleWrapper .slider").click()
                print("Double points enabled.")
            else:
                print("Double points already enabled.")

            return True

        except Exception as e:
            print("Failed to enable double points:", e)
            return False
    
    def click_next_required_package(page):
        dismiss_modals(page)
        package_selector = ".pTableRow, .pkgCard, tr.pTableRow, tr.packageTableRow, .packageTableRow"
        try:
            page.wait_for_selector(package_selector, timeout=10000)
        except Exception:
            return None

        try:
            pkg_info = page.evaluate("""() => {
                const cards = Array.from(document.querySelectorAll('.pkgCard[data-status="todo"], .pTableRow[data-status="todo"]'));
                const unlocked = cards.filter(c => {
                    const pkgSpan = c.querySelector('.package');
                    return !pkgSpan || pkgSpan.getAttribute('is_locked') !== '1';
                });
                if (unlocked.length === 0) return null;

                let target = unlocked.find(c => c.classList.contains('pkgCard--next')) || unlocked[0];
                const nameElem = target.querySelector('.pkgCard-name, .package-name');
                const name = nameElem ? nameElem.innerText.trim() : 'Unknown';

                const btn = target.querySelector('.pkgBtn--run, .pkgBtn, a[id^="btnRun"], a.pkgBtn') || target.querySelector('a');
                if (!btn) return null;
                const href = btn.getAttribute('href') || '';
                const visible = btn.offsetParent !== null;
                if (visible) {
                    btn.click();
                }
                return { name: name, remaining: unlocked.length, href: href, clicked: visible };
            }""")
            if pkg_info and not pkg_info.get("clicked") and pkg_info.get("href"):
                page.goto(pkg_info["href"])
            try:
                page.wait_for_url("**/practice/**", timeout=15000)
            except Exception:
                pass
            return pkg_info
        except Exception:
            return None

    def click_package_by_index(page, index: int):
        dismiss_modals(page)
        package_selector = ".pTableRow, .pkgCard, tr.pTableRow, tr.packageTableRow, .packageTableRow"
        try:
            page.wait_for_selector(package_selector, timeout=10000)
        except Exception:
            page.screenshot(path="debug_packages_timeout.png")
            print(f"\n[!] Error: Timed out waiting for package elements ({package_selector}).")
            print(f"[!] Current page URL: {page.url}")
            print("[!] A debug screenshot has been saved to 'debug_packages_timeout.png'.")
            print("\nPossible reasons:")
            print(f" 1. The selected class (index {class_index}) might have no active packages or assignments.")
            print(" 2. A popup, competition announcement, or cookie banner blocked the page.")
            print(" 3. The class index in config.toml points to the wrong class.\n")
            return False

        packages = page.locator(package_selector)

        count = packages.count()
        if index < 0 or index >= count:
            print(f"Package index {index} out of range. Found {count} packages.")
            return False

        print(f"\nAvailable package(s) ({count} found):")
        for i in range(count):
            txt = packages.nth(i).inner_text().strip().replace("\n", " ")
            print(f"  [{i}] {txt[:70]}")

        print(f"\nClicking package at index {index}...")

        # Click practice/run button inside that package card/row
        btn = packages.nth(index).locator(".pkgBtn--run, .pkgBtn, a .btn-primary, a.btn-primary, .btn-primary, a:has-text('Spustit'), a:has-text('Spustiť'), a:has-text('Precvičovať'), a:has-text('Practice')").first
        if btn.count() > 0:
            btn.click()
        else:
            packages.nth(index).locator("a").first.click()

        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        return True
    
    def click_class_by_index(page, index: int):
        dismiss_modals(page)
        try:
            # Wait for the class list to appear
            page.wait_for_selector("#listOfClasses", timeout=10000)

            # Get all class links
            class_links = page.locator("#listOfClasses a").all()

            if not class_links:
                print("No classes found under #listOfClasses.")
                return False

            print(f"\nAvailable class link(s) ({len(class_links)} found):")
            for i, link in enumerate(class_links):
                txt = link.inner_text().strip().replace("\n", " ")
                print(f"  [{i}] {txt}")

            if index < 0 or index >= len(class_links):
                print(f"Class index {index} out of range. Please choose between 0 and {len(class_links) - 1}.")
                return False

            print(f"\nClicking class at index {index}...")
            class_links[index].click()
            page.wait_for_load_state("domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            dismiss_modals(page)
            return True

        except Exception as e:
            print("Error clicking class:", e)
            page.screenshot(path="debug_class_error.png")
            return False
    
    p = sync_playwright().start()

    launch_args = [
        "--no-sandbox",
        "--remote-debugging-port=9222"
    ]
    if not headless:
        launch_args.append("--start-maximized")

    browser = p.chromium.launch(
        headless=headless,
        args=launch_args
    )

    if not headless:
        context = browser.new_context(no_viewport=True)
    else:
        context = browser.new_context()

    page = context.new_page()

    # navigate to target
    if not urlbase.startswith(("http://", "https://")):
        target = "https://" + urlbase
    else:
        target = urlbase

    page.goto(target)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    print("Opened page:", page.url)

    # fill login fields
    try:
        page.wait_for_selector("#login", timeout=5000)
        page.fill("#login", username)
        print("Filled #login")
    except Exception:
        print("Login input (#login) not found")

    try:
        page.wait_for_selector("#password", timeout=5000)
        page.fill("#password", password)
        print("Filled #password")
        # submit (press Enter)
        page.press("#password", "Enter")
        print("Submitted credentials")
    except Exception:
        print("Password input (#password) not found")
    
    # Wait for navigation after login
    page.wait_for_load_state("networkidle")
    dismiss_modals(page)

    if not click_class_by_index(page, class_index):
        print("Failed to click class. Aborting startup.")
        browser.close()
        p.stop()
        sys.exit(1)
    
    if complete_all_required:
        pkg_info = click_next_required_package(page)
        if not pkg_info:
            print("No required (todo) homework packages found. All are already completed!")
            browser.close()
            p.stop()
            sys.exit(0)
        pkg_name = pkg_info.get("name", "Unknown")
        rem = pkg_info.get("remaining", 1)
        print(f"\n[+] Starting next required package: '{pkg_name}' ({rem} to do)")
    else:
        if not click_package_by_index(page, package_index):
            print("Failed to click package. Aborting startup.")
            browser.close()
            p.stop()
            sys.exit(1)
    
    if double_points:
        enable_double_points(page)
    
    solver_proc = subprocess.Popen([sys.executable, "-u", "solver.py"])

    try:
        solver_proc.wait()   # Wait until solver exits
    finally:
        print("Solver exited. Cleaning up...")

        browser.close()
        p.stop()


if __name__ == "__main__":
    main()

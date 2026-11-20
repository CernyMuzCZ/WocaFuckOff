#!/usr/bin/env python3
import sys
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from playwright.sync_api import sync_playwright
import time
import random
import requests
import unicodedata
import json
import os
import traceback
import toml
import argparse

# ----------------- CLI -----------------
parser = argparse.ArgumentParser()
parser.add_argument("--url", help="Target URL")
args, _ = parser.parse_known_args()

# ----------------- Config file -----------------
config_file = "config.toml"
config = toml.load(config_file) if os.path.exists(config_file) else {}

# ----------------- Variables -----------------
URLBASE = args.url or os.environ.get("URLBASE") or config.get("urlbase", "wocabee.app/app")
if not URLBASE.startswith(("http://", "https://")):
    URLBASE = "https://" + URLBASE

DEBUG_PORT = os.environ.get("DEBUG_PORT") or config.get("debug_port", "http://127.0.0.1:9222")
if DEBUG_PORT and DEBUG_PORT.isdigit():
    DEBUG_PORT = f"http://127.0.0.1:{DEBUG_PORT}"

WORDLIST_FILE = os.environ.get("WORDLIST_FILE") or config.get("wordlist_file", "wordlist.json")
PICTURE_FILE = os.environ.get("PICTURE_FILE") or config.get("picture_file", "picturelist.json")

addon_points = int(os.environ.get("ADDON_POINTS") or config.get("addon_points", -1))
milestone_reminder = int(os.environ.get("MILESTONE_REMINDER") or config.get("milestone_reminder", 1000))

PLACEHOLDER_WORDS = {
    w.strip() for w in (
        os.environ.get("PLACEHOLDER_WORDS")
        or ",".join(config.get("placeholder_words", ["", "translate", "check"]))
    ).split(",")
}
NTFY_SERVER = (os.environ.get("NTFY_SERVER") or config.get("ntfy_server") or "").strip() or None
NTFY_TOPIC  = (os.environ.get("NTFY_TOPIC")  or config.get("ntfy_topic")  or "").strip() or None
NTFY_TOKEN  = (os.environ.get("NTFY_TOKEN")  or config.get("ntfy_token")  or "").strip() or None
HEADLESS    = str(os.environ.get("HEADLESS") or config.get("headless", False)).lower() in ("true", "1", "yes")
AUTO_LEARN  = str(os.environ.get("AUTO_LEARN") or config.get("auto_learn", True)).lower() in ("true", "1", "yes")
COMPLETE_ALL_REQUIRED = str(os.environ.get("COMPLETE_ALL_REQUIRED") or config.get("complete_all_required", True)).lower() in ("true", "1", "yes")
DOUBLE_POINTS = str(os.environ.get("DOUBLE_POINTS") or config.get("double_points", False)).lower() in ("true", "1", "yes")

if not URLBASE or not DEBUG_PORT:
    print("Error: URLBASE and DEBUG_PORT must be set via environment variables, config.toml, or CLI arguments.")
    exit(1)

# ----------- NTFY ERROR FUNCTION ------------

def notify_ntfy(title: str, message: str):
    if not NTFY_SERVER or "example.ntfy.server" in NTFY_SERVER or not NTFY_TOPIC:
        return
    try:
        url = f"{NTFY_SERVER}/{NTFY_TOPIC}"
        data = message.encode("utf-8")
        headers = {"Title": title}
        if NTFY_TOKEN:
            headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
        response = requests.post(url, data=data, headers=headers, timeout=5)
        response.raise_for_status()
    except Exception as e:
        print("Failed to send NTFY notification:", e)

# ---------------- LOAD DATA ----------------

if os.path.exists(WORDLIST_FILE):
    with open(WORDLIST_FILE, "r", encoding="utf-8") as f:
        WORD_TABLE = json.load(f)
else:
    WORD_TABLE = {}
    with open(WORDLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(WORD_TABLE, f, ensure_ascii=False, indent=2)

if os.path.exists(PICTURE_FILE):
    with open(PICTURE_FILE, "r", encoding="utf-8") as f:
        PICTURE_MAP = json.load(f)
else:
    PICTURE_MAP = {}

def save_word_table():
    try:
        with open(WORDLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(WORD_TABLE, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def save_picture_map():
    try:
        with open(PICTURE_FILE, "w", encoding="utf-8") as f:
            json.dump(PICTURE_MAP, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# ---------------- HELPERS ----------------

def strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )

def normalize(text: str) -> str:
    if not text:
        return ""
    text = text.strip().lower()
    text = strip_accents(text)
    return text

def find_target_page(context):
    for page in context.pages:
        if URLBASE in page.url:
            return page
    return None

def safe_click(page, locator, timeout=2500) -> bool:
    """
    Attempts a genuine Playwright mouse click first (so screenX/screenY and normal
    browser events fire), falling back to force click and DOM click if needed.
    """
    try:
        locator.click(timeout=timeout)
        return True
    except Exception:
        try:
            locator.click(force=True, timeout=1500)
            return True
        except Exception:
            try:
                h = locator.element_handle(timeout=1000)
                if h:
                    page.evaluate("(el) => el.click()", h)
                    return True
            except Exception:
                return False
    return False

def type_and_submit(page, input_selector: str, submit_selector: str, answer: str) -> bool:
    """
    Clears an input, types the answer with realistic keystrokes, dispatches keyup/change
    (critical when an answer ends with a non-ASCII character like á/ý/ß/ä/č/ř), waits
    for the submit button to become enabled/visible, and clicks it.
    """
    input_elem = page.locator(input_selector)
    submit_elem = page.locator(submit_selector)
    if input_elem.count() == 0:
        return False

    safe_click(page, input_elem)
    try:
        input_elem.fill("")
    except Exception:
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")

    page.keyboard.type(answer, delay=random.randint(35, 75))

    # Non-ASCII chars in Playwright keyboard.type use insertText (which fires input, not keyup).
    # WocaBee's ir() / wr() listen on keyup & change, so dispatch them explicitly.
    try:
        input_elem.dispatch_event("keyup")
        input_elem.dispatch_event("change")
    except Exception:
        pass

    # Wait up to 2.5s for submit button to become visible & enabled
    max_wait = 2.5
    elapsed = 0.0
    while elapsed < max_wait:
        try:
            if submit_elem.count() > 0 and submit_elem.is_visible() and submit_elem.is_enabled():
                break
        except Exception:
            pass
        time.sleep(0.05)
        elapsed += 0.05

    if submit_elem.count() > 0 and submit_elem.is_visible() and submit_elem.is_enabled():
        safe_click(page, submit_elem)
    else:
        page.keyboard.press("Enter")

    return True

def get_answer_auto_update(word: str) -> str:
    normalized_word = normalize(word)

    # Direct match
    answer = WORD_TABLE.get(normalized_word)
    if answer:
        return answer

    # Reverse lookup
    for sk, en in WORD_TABLE.items():
        if normalize(en) == normalized_word:
            return sk

    # Multi-word
    if "," in normalized_word:
        for w in [w.strip() for w in normalized_word.split(",")]:
            answer = WORD_TABLE.get(w)
            if answer:
                return answer
            for sk, en in WORD_TABLE.items():
                if normalize(en) == w:
                    return sk

    if HEADLESS or AUTO_LEARN:
        print(f"Auto-learn mode — unknown word '{word}', submitting as-is for auto-learn")
        return word

    answer = input(f"Enter translation for unknown word '{word}': ").strip()
    if answer:
        answer = normalize(answer)
        WORD_TABLE[normalized_word] = answer
        save_word_table()
        print(f"New word added: '{word}' -> '{answer}'")
    return answer

def get_current_points(page):
    for sel in ["#WocaPoints", ".sc-wp-txt", ".rankSummary-wp", ".wocaPointsRewardValue"]:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                txt = loc.first.inner_text().strip()
                digits = "".join(c for c in txt if c.isdigit())
                if digits:
                    return int(digits)
        except Exception:
            pass
    return None

def get_in_page_answer(page):
    try:
        val = page.evaluate("() => { const el = document.querySelector('#a_word'); return (el && el.value) ? el.value.trim() : null; }")
        return val if val else None
    except Exception:
        return None

def get_in_page_word_id(page):
    try:
        val = page.evaluate("() => { const el = document.querySelector('#word_id'); return (el && el.value) ? el.value.trim() : null; }")
        return val if val else None
    except Exception:
        return None

def get_picture_answer_fallback(page, src: str):
    """Fallback lookup for picture exercises via #pictures + $locWords or PICTURE_MAP."""
    if src and src in PICTURE_MAP:
        return PICTURE_MAP[src]
    try:
        res = page.evaluate("""(targetSrc) => {
            const pics = Array.from(document.querySelectorAll('#pictures .picture'));
            const match = pics.find(p => p.getAttribute('src') === targetSrc);
            if (!match) return null;
            const wid = match.getAttribute('word_id');
            if (wid && typeof $locWords !== 'undefined' && Array.isArray($locWords)) {
                const entry = $locWords.find(w => String(w.word_id) === String(wid));
                if (entry) return entry.translation || entry.word || null;
            }
            return match.getAttribute('word') || null;
        }""", src or "")
        if res:
            return res.strip()
    except Exception:
        pass
    return None

def find_best_choice_index(page, selector: str, target_answer: str, exact_case_first: bool = False, include_hidden: bool = False):
    """
    Matches `target_answer` against elements matching `selector`.
    Uses the first text node first (matching WocaBee's $(this).contents().get(0).nodeValue)
    so appended notes in child tags do not break matching.
    Returns (index, matched_text) or (None, None).
    """
    items = page.evaluate("""({sel, includeHidden}) => {
        const els = Array.from(document.querySelectorAll(sel));
        return els.map((el, idx) => {
            const firstNode = (el.childNodes && el.childNodes[0] && el.childNodes[0].nodeType === 3)
                ? el.childNodes[0].nodeValue.trim()
                : '';
            const fullText = (el.textContent || '').trim();
            const visible = el.offsetParent !== null && getComputedStyle(el).visibility !== 'hidden';
            return { idx, firstText: firstNode || fullText, fullText, visible };
        }).filter(item => includeHidden || item.visible);
    }""", {"sel": selector, "includeHidden": include_hidden})

    if not items or not target_answer:
        return None, None

    target_raw = target_answer.strip()
    target_lower = target_raw.lower()
    target_norm = normalize(target_raw)

    # Pass 1: Exact raw match (critical for chooseSpelling: accents & case!)
    for it in items:
        if it["firstText"] == target_raw or it["fullText"] == target_raw:
            return it["idx"], it["firstText"]

    # Pass 2: Case-insensitive exact match (preserves accents)
    for it in items:
        if it["firstText"].lower() == target_lower or it["fullText"].lower() == target_lower:
            return it["idx"], it["firstText"]

    # Pass 3: Accent-stripped normalized exact match
    for it in items:
        if normalize(it["firstText"]) == target_norm or normalize(it["fullText"]) == target_norm:
            return it["idx"], it["firstText"]

    if not exact_case_first:
        # Pass 4: Comma-separated exact parts match
        expected_parts = [normalize(p) for p in target_raw.split(",") if p.strip()]
        for it in items:
            fn = normalize(it["firstText"])
            if fn in expected_parts:
                return it["idx"], it["firstText"]
            item_parts = [normalize(p) for p in it["firstText"].split(",") if p.strip()]
            if any(ep in item_parts for ep in expected_parts):
                return it["idx"], it["firstText"]

        # Pass 5: Substring match only for longer strings (>= 3 chars) to avoid false matches like 'a' in 'also'
        for it in items:
            fn = normalize(it["firstText"])
            for ep in expected_parts:
                if len(ep) >= 3 and len(fn) >= 3 and (ep in fn or fn in ep):
                    return it["idx"], it["firstText"]

    return None, None

LAST_EXTRACTED_PACKAGE = None

def extract_words_from_page(page):
    """
    Extracts complete package vocabulary from WocaBee's in-page $locWords JavaScript variable
    and picture mappings from #pictures. Only extracts once per package to prevent log spam.
    """
    global LAST_EXTRACTED_PACKAGE
    try:
        pkg_id = page.evaluate("""() => {
            if (typeof $locWords !== 'undefined' && $locWords && $locWords.length > 0) {
                return $locWords[0].package_id || null;
            }
            return null;
        }""")
        if not pkg_id:
            if "package_id=" in page.url:
                pkg_id = page.url.split("package_id=")[1].split("&")[0]
            else:
                pkg_id = page.url

        if pkg_id == LAST_EXTRACTED_PACKAGE:
            return

        loc_words = page.evaluate("() => typeof $locWords !== 'undefined' ? $locWords : null")
        if loc_words:
            added = 0
            for item in loc_words:
                w = (item.get("word") or "").strip()
                tr = (item.get("translation") or "").strip()
                if w and tr:
                    norm_w = normalize(w)
                    norm_tr = normalize(tr)
                    if norm_w not in WORD_TABLE or WORD_TABLE[norm_w] != norm_tr:
                        WORD_TABLE[norm_w] = norm_tr
                        added += 1
            if added > 0:
                save_word_table()
                print(f"Loaded {added} words from package vocabulary ($locWords) into {WORDLIST_FILE}")

            # Also cache picture src -> translation/word mappings from #pictures
            pics = page.evaluate("""() => {
                const els = Array.from(document.querySelectorAll('#pictures .picture'));
                return els.map(el => ({
                    word_id: el.getAttribute('word_id'),
                    word: el.getAttribute('word'),
                    src: el.getAttribute('src')
                }));
            }""")
            if pics:
                pic_added = 0
                wid_to_trans = {str(item.get("word_id")): (item.get("translation") or "").strip() for item in loc_words if item.get("word_id")}
                for p in pics:
                    src = p.get("src")
                    wid = str(p.get("word_id") or "")
                    tr = wid_to_trans.get(wid) or (p.get("word") or "").strip()
                    if src and tr and PICTURE_MAP.get(src) != tr:
                        PICTURE_MAP[src] = tr
                        pic_added += 1
                if pic_added > 0:
                    save_picture_map()

            LAST_EXTRACTED_PACKAGE = pkg_id
    except Exception:
        pass

# ---------------- SYSTEM / TRANSITION HANDLERS ----------------

def handle_transient_screens(page) -> bool:
    """
    Handles animations, saving modals, retry buttons, and accidental quit screens.
    Returns True if the bot should wait/continue without trying to answer an exercise.
    """
    try:
        # 1. Saving package modal (#modalSavingPackage)
        saving_modal = page.locator("#modalSavingPackage")
        if saving_modal.count() > 0 and saving_modal.is_visible():
            retry_btn = page.locator("#saveAgainBtn")
            if retry_btn.count() > 0 and retry_btn.is_visible():
                is_disabled = retry_btn.evaluate("el => el.classList.contains('disabled') || el.disabled")
                if not is_disabled:
                    print("Saving failed modal detected — clicking 'Zkusit znovu' (#saveAgainBtn)...")
                    safe_click(page, retry_btn)
                    page.wait_for_timeout(1000)
                    return True
            page.wait_for_timeout(350)
            return True

        # 2. Accidental homework quit screen (#homeworkQuitScreen) -> resume homework
        quit_screen = page.locator("#homeworkQuitScreen")
        if quit_screen.count() > 0 and quit_screen.is_visible():
            resume_btn = page.locator("#doNotLeaveHomeworkBtn")
            if resume_btn.count() > 0 and resume_btn.is_visible():
                print("Homework quit screen detected — clicking Pokračovat to resume...")
                safe_click(page, resume_btn)
                page.wait_for_timeout(400)
                return True

        # 3. Result/celebration overlays mid-fade (#correct, #halfStageReached, #wocaPointsReward, #wbpCheckpoint)
        for sel in ["#correct", "#halfStageReached", "#wocaPointsReward", "#wbpCheckpoint"]:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.is_visible():
                page.wait_for_timeout(250)
                return True
    except Exception:
        pass
    return False

def handle_intro_and_continue(page) -> bool:
    """
    Advances intro cover (#introRun), intro cards (#introNext), problem words review
    (#problem-words-next), and milestone/review continue buttons (#continueBtn).
    """
    try:
        # Wait if intro container is mid-fade so we don't race xa()
        is_animating = page.evaluate("""() => {
            if (!window.jQuery) return false;
            return jQuery('#introContainer').is(':animated') || jQuery('#introLabelWrapper').is(':animated');
        }""")
        if is_animating:
            page.wait_for_timeout(200)
            return True

        target_sel = page.evaluate("""() => {
            const ids = ['#introNext', '#introRun', '#continueBtn', '#problem-words-next'];
            for (const id of ids) {
                const el = document.querySelector(id);
                if (el && el.offsetParent !== null && getComputedStyle(el).visibility !== 'hidden') {
                    return id;
                }
            }
            return null;
        }""")

        if target_sel:
            btn = page.locator(target_sel).first
            # Wait briefly if WocaBee temporarily disabled the button (e.g., 1.25s audio lock)
            if not btn.is_enabled():
                waited = 0.0
                while waited < 1.6 and btn.is_visible() and not btn.is_enabled():
                    time.sleep(0.1)
                    waited += 0.1
                if btn.is_visible() and not btn.is_enabled():
                    page.evaluate("""(sel) => {
                        const el = document.querySelector(sel);
                        if (el) { el.removeAttribute('disabled'); el.disabled = false; }
                    }""", target_sel)

            if btn.is_visible():
                print(f"Advancing screen: clicking {target_sel}")
                safe_click(page, btn)
                page.wait_for_timeout(450)
                return True
    except Exception:
        pass
    return False

def handle_incorrect_autolearn(page) -> bool:
    """
    When the #incorrect feedback div is visible, save the correct word (and picture if shown)
    and click #incorrect-next-button.
    """
    incorrect_div = page.locator("#incorrect")
    if incorrect_div.count() == 0 or not incorrect_div.is_visible():
        return False

    try:
        correct_question_elem = incorrect_div.locator(".correctWordQuestion")
        correct_answer_elem = incorrect_div.locator(".correctWordAnswer")

        if correct_question_elem.count() > 0 and correct_answer_elem.count() > 0:
            correct_question = normalize(correct_question_elem.inner_text().strip())
            correct_answer_raw = correct_answer_elem.inner_text().strip()
            correct_answer = normalize(correct_answer_raw)

            if correct_question and correct_answer:
                if WORD_TABLE.get(correct_question) != correct_answer:
                    WORD_TABLE[correct_question] = correct_answer
                    save_word_table()
                    print(f"Auto-learned: '{correct_question}' -> '{correct_answer}'")

            # If #resultPicture is visible, learn its src too
            res_pic = page.locator("#resultPicture")
            if res_pic.count() > 0 and res_pic.is_visible():
                src = res_pic.get_attribute("src")
                if src and not src.startswith("data:,") and correct_answer_raw:
                    PICTURE_MAP[src] = correct_answer_raw
                    save_picture_map()

        next_btn = page.locator("#incorrect-next-button")
        if next_btn.count() > 0 and next_btn.is_visible():
            safe_click(page, next_btn)
            print("Auto-learn: clicked #incorrect-next-button")
            page.wait_for_timeout(400)
            return True
    except Exception as e:
        print("handle_incorrect_autolearn error:", e)

    return False

def check_stage_or_homework_finished(page) -> bool:
    """
    Detects when a homework stage (1/3, 2/3, or 3/3) has completed and saved:
    - After ma() saves, #backBtn is replaced with #saveAndLeaveBtn ("Uložit a odejít", contains .fa-save).
    - If problem words exist, #continueBtn / #problem-words-next are shown first (handled before this).
    - Once #continueBtn and #problem-words-next are gone and #backBtn shows save-and-leave
      (or #msgCompleted / #leaveBtnWrapper is visible), click it to return to the class page.
    """
    try:
        action = page.evaluate("""() => {
            const contBtn = document.querySelector('#continueBtn');
            const pwNext = document.querySelector('#problem-words-next');
            const modal = document.querySelector('#modalSavingPackage');

            if (contBtn && contBtn.offsetParent !== null) return null;
            if (pwNext && pwNext.offsetParent !== null) return null;
            if (modal && modal.offsetParent !== null) return null;

            const leaveWrapper = document.querySelector('#leaveBtnWrapper');
            if (leaveWrapper && leaveWrapper.offsetParent !== null) {
                return '#leaveBtnWrapper';
            }

            const backBtn = document.querySelector('#backBtn');
            if (backBtn && backBtn.offsetParent !== null) {
                const txt = (backBtn.innerText || '').toLowerCase();
                const hasSaveIcon = backBtn.querySelector('.fa-save') !== null;
                const msg = document.querySelector('#msgCompleted');
                const msgVisible = msg && msg.offsetParent !== null;

                if (hasSaveIcon || txt.includes('uložit') || txt.includes('uložiť') || txt.includes('save') || msgVisible) {
                    return '#backBtn';
                }
            }
            return null;
        }""")

        if action:
            print(f"Stage/homework saved & finished! Clicking {action} to return to class page...")
            safe_click(page, page.locator(action).first)
            page.wait_for_timeout(1500)
            return True
    except Exception:
        pass
    return False

# ---------------- ALL 15 EXERCISE HANDLERS ----------------

# 1. choosePicture (#choosePicture) — supports both new .wbps slider and legacy .slick slider
def handle_choose_picture(page) -> bool:
    container = page.locator("#choosePicture")
    if container.count() == 0 or not container.is_visible():
        return False

    target_wid = get_in_page_word_id(page)
    target_a_word = get_in_page_answer(page)
    q_word_raw = ""
    q_elem = page.locator("#choosePictureWord")
    if q_elem.count() > 0:
        q_word_raw = q_elem.inner_text().strip()

    fallback_word = None
    if not target_a_word and q_word_raw:
        fallback_word = get_answer_auto_update(q_word_raw)

    # Gather all slides and images inside #word-img-container
    slider_info = page.evaluate("""() => {
        const root = document.querySelector('#word-img-container');
        if (!root) return null;
        const isWbps = root.classList.contains('wbps') || root.querySelector('.wbps-slide') !== null;
        const isSlick = !isWbps && root.querySelector('.slick-slide') !== null;

        let imgs = [];
        if (isWbps) {
            imgs = Array.from(root.querySelectorAll('.wbps-slide .picture'));
        } else if (isSlick) {
            imgs = Array.from(root.querySelectorAll('.slick-slide:not(.slick-cloned) .picture'));
        } else {
            imgs = Array.from(root.querySelectorAll('.picture'));
        }

        const items = imgs.map((im, idx) => ({
            idx,
            word_id: (im.getAttribute('word_id') || '').trim(),
            word: (im.getAttribute('word') || '').trim(),
            src: (im.getAttribute('src') || '').trim()
        }));

        // Check if #pictures has a matching src for target_wid
        let expectedSrc = null;
        const widEl = document.querySelector('#word_id');
        if (widEl && widEl.value) {
            const p = document.querySelector('#pictures .picture[word_id="' + widEl.value.trim() + '"]');
            if (p) expectedSrc = p.getAttribute('src');
        }

        return { isWbps, isSlick, items, expectedSrc };
    }""")

    if not slider_info or not slider_info.get("items"):
        return False

    items = slider_info["items"]
    expected_src = slider_info.get("expectedSrc")
    target_idx = None

    # Priority 1: Exact word_id match
    if target_wid and target_wid != "0":
        for it in items:
            if it["word_id"] == target_wid:
                target_idx = it["idx"]
                break

    # Priority 2: Exact word attribute match against #a_word
    if target_idx is None and target_a_word:
        for it in items:
            if it["word"] == target_a_word:
                target_idx = it["idx"]
                break
        if target_idx is None:
            norm_a = normalize(target_a_word)
            for it in items:
                if normalize(it["word"]) == norm_a:
                    target_idx = it["idx"]
                    break

    # Priority 3: Expected src from #pictures
    if target_idx is None and expected_src:
        for it in items:
            if it["src"] == expected_src:
                target_idx = it["idx"]
                break

    # Priority 4: Wordlist / PICTURE_MAP fallback
    if target_idx is None and fallback_word:
        norm_fb = normalize(fallback_word)
        for it in items:
            if normalize(it["word"]) == norm_fb:
                target_idx = it["idx"]
                break

    if target_idx is None and q_word_raw:
        norm_q = normalize(q_word_raw)
        for it in items:
            mapped = PICTURE_MAP.get(it["src"])
            if mapped and normalize(mapped) == norm_q:
                target_idx = it["idx"]
                break

    if target_idx is None:
        target_idx = 0

    chosen = items[target_idx]
    if chosen["src"] and q_word_raw and not chosen["src"].startswith("data:,"):
        PICTURE_MAP[chosen["src"]] = q_word_raw
        save_picture_map()

    # Navigate carousel to target_idx
    if slider_info["isWbps"]:
        dots = page.locator("#word-img-container .wbps-dot")
        if dots.count() > target_idx:
            is_on = dots.nth(target_idx).evaluate("el => el.classList.contains('wbps-on')")
            if not is_on:
                safe_click(page, dots.nth(target_idx))
                # Wait for smooth scroll-snap to activate target dot
                waited = 0.0
                while waited < 1.2:
                    if dots.nth(target_idx).evaluate("el => el.classList.contains('wbps-on')"):
                        break
                    time.sleep(0.08)
                    waited += 0.08
                time.sleep(0.18)
        # Ensure target slide is centered in view
        page.evaluate("""(idx) => {
            const slides = document.querySelectorAll('#word-img-container .wbps-slide');
            if (slides[idx]) slides[idx].scrollIntoView({ behavior: 'instant', inline: 'center', block: 'nearest' });
        }""", target_idx)
        time.sleep(0.1)
        target_img = page.locator("#word-img-container .wbps-slide .picture").nth(target_idx)

    elif slider_info["isSlick"]:
        dots = page.locator("#word-img-container .slick-dots li button")
        if dots.count() > target_idx:
            is_active = page.locator("#word-img-container .slick-dots li").nth(target_idx).evaluate("el => el.classList.contains('slick-active')")
            if not is_active:
                safe_click(page, dots.nth(target_idx))
                time.sleep(0.65)
        target_img = page.locator("#word-img-container .slick-slide:not(.slick-cloned) .picture").nth(target_idx)
    else:
        target_img = page.locator("#word-img-container .picture").nth(target_idx)

    # Double-click target image (vr() requires 2 clicks within 1500ms)
    print(f"ChoosePicture: selecting slide {target_idx} ('{chosen['word']}', word_id={chosen['word_id']}) for '{q_word_raw}'")
    safe_click(page, target_img)
    time.sleep(0.12)
    if container.is_visible():
        safe_click(page, target_img)
    page.wait_for_timeout(450)
    return True

# 2. describePicture (#describePicture)
def handle_describe_picture(page) -> bool:
    container = page.locator("#describePicture")
    if container.count() == 0 or not container.is_visible():
        return False

    img_elem = page.locator("#describePictureImg")
    input_elem = page.locator("#describePictureAnswer")
    if img_elem.count() == 0 or input_elem.count() == 0:
        return False

    src = img_elem.get_attribute("src") or ""
    answer = get_in_page_answer(page) or get_picture_answer_fallback(page, src)

    if not answer:
        if AUTO_LEARN or HEADLESS:
            print("DescribePicture: unknown image, submitting placeholder for auto-learn")
            answer = "guess"
        else:
            answer = input(f"Enter word for image {src}: ").strip()

    if src and answer and answer != "guess":
        PICTURE_MAP[src] = answer
        save_picture_map()

    type_and_submit(page, "#describePictureAnswer", "#describePictureSubmitBtn", answer)
    print("DescribePicture answered:", answer)
    page.wait_for_timeout(400)
    return True

# 3. addMissingWord (#addMissingWord)
def handle_add_missing_word(page) -> bool:
    container = page.locator("#addMissingWord")
    if container.count() == 0 or not container.is_visible():
        return False

    missing_word = page.evaluate("""() => {
        const full = (document.querySelector('#a_word')?.value || '').trim();
        if (!full) return null;
        const stripped = full.replace(/[. \\s!?]+$/g, '');
        const words = stripped.split(' ');
        const sentenceEl = document.querySelector('#a_sentence');
        const dispEl = document.querySelector('#missingWordDisp');
        if (!sentenceEl || !dispEl) return null;

        let beforeText = '';
        for (const node of sentenceEl.childNodes) {
            if (node === dispEl) break;
            beforeText += node.textContent || '';
        }
        const trimmedBefore = beforeText.trim();
        const wordIdx = trimmedBefore === '' ? 0 : trimmedBefore.split(/\\s+/).length;
        let result = words[wordIdx] || '';

        const noOfChars = parseInt(dispEl.getAttribute('noOfChars') || '0', 10);
        if (noOfChars > 0 && result.length !== noOfChars) {
            const byLen = words.find(w => w.length === noOfChars);
            if (byLen) result = byLen;
        }
        return result;
    }""")

    if not missing_word:
        # Fallback: try matching from #q_sentence via WORD_TABLE
        q_sent = page.locator("#q_sentence").inner_text().strip() if page.locator("#q_sentence").count() > 0 else ""
        full_ans = get_answer_auto_update(q_sent) if q_sent else ""
        if full_ans:
            no_chars = page.evaluate("() => parseInt(document.querySelector('#missingWordDisp')?.getAttribute('noOfChars') || '0', 10)")
            words = full_ans.rstrip(". !?").split()
            missing_word = next((w for w in words if len(w) == no_chars), words[0] if words else "a" * max(1, no_chars))
        else:
            return False

    print(f"AddMissingWord: typing missing word '{missing_word}'")
    type_and_submit(page, "#missingWordAnswer", "#addMissingWordSubmitBtn", missing_word)
    page.wait_for_timeout(400)
    return True

# 4. arrangeWords (#arrangeWords)
def handle_arrange_words(page) -> bool:
    container = page.locator("#arrangeWords")
    if container.count() == 0 or not container.is_visible():
        return False

    target_sentence = get_in_page_answer(page)
    if not target_sentence:
        q_elem = page.locator("#def-lang-sentence")
        if q_elem.count() > 0:
            target_sentence = get_answer_auto_update(q_elem.inner_text().strip())

    if target_sentence:
        reordered = page.evaluate("""(full) => {
            const expected = full.trim().replace(/[. \\s!?]+$/g, '').split(' ').filter(Boolean);
            const container = document.querySelector('#sortableWords');
            if (!container) return false;
            const punct = container.querySelector('#static-punctuation');
            const tiles = Array.from(container.querySelectorAll('.word-to-arrange'));
            if (tiles.length === 0) return false;

            const used = new Set();
            const ordered = [];
            for (const w of expected) {
                let idx = tiles.findIndex((t, i) => !used.has(i) && t.getAttribute('word') === w);
                if (idx === -1) {
                    idx = tiles.findIndex((t, i) => !used.has(i) && (t.getAttribute('word') || '').toLowerCase() === w.toLowerCase());
                }
                if (idx !== -1) {
                    used.add(idx);
                    ordered.push(tiles[idx]);
                }
            }
            tiles.forEach((t, i) => { if (!used.has(i)) ordered.push(t); });
            ordered.forEach(el => container.appendChild(el));
            if (punct) container.appendChild(punct);

            if (window.jQuery && jQuery(container).data('ui-sortable')) {
                jQuery(container).sortable('refresh');
            }
            return true;
        }""", target_sentence)
        if reordered:
            print(f"ArrangeWords: arranged '{target_sentence}'")

    time.sleep(random.uniform(0.15, 0.3))
    submit_btn = page.locator("#arrangeWordsSubmitBtn")
    if submit_btn.count() > 0 and submit_btn.is_visible():
        safe_click(page, submit_btn)
        page.wait_for_timeout(500)
        return True
    return False

# 5. translateWord (#translateWord)
def handle_translate_word(page) -> bool:
    container = page.locator("#translateWord")
    if container.count() == 0 or not container.is_visible():
        return False

    q_span = page.locator("#q_word")
    word_raw = q_span.inner_text().strip() if q_span.count() > 0 else ""
    word = normalize(word_raw)
    if word in PLACEHOLDER_WORDS and not get_in_page_answer(page):
        return False

    answer = get_in_page_answer(page) or get_answer_auto_update(word)
    if not answer:
        return False

    print(f"TranslateWord: '{word_raw}' -> '{answer}'")
    type_and_submit(page, "#translateWordAnswer", "#translateWordSubmitBtn", answer)
    page.wait_for_timeout(400)
    return True

# 6. translateFallingWord (#translateFallingWord)
def handle_translate_falling_word(page) -> bool:
    container = page.locator("#translateFallingWord")
    if container.count() == 0 or not container.is_visible():
        return False

    word_elem = page.locator("#tfw_word")
    word_raw = word_elem.inner_text().strip() if word_elem.count() > 0 else ""
    word = normalize(word_raw)
    if word in PLACEHOLDER_WORDS and not get_in_page_answer(page):
        return False

    answer = get_in_page_answer(page) or get_answer_auto_update(word)
    if not answer:
        print(f"TranslateFallingWord: no answer found for '{word_raw}'")
        return False

    print(f"TranslateFallingWord: '{word_raw}' -> '{answer}'")
    type_and_submit(page, "#translateFallingWordAnswer", "#translateFallingWordSubmitBtn", answer)
    page.wait_for_timeout(400)
    return True

# 7. transcribe (#transcribe)
def handle_transcribe(page) -> bool:
    container = page.locator("#transcribe")
    if container.count() == 0 or not container.is_visible():
        return False

    answer = get_in_page_answer(page)
    if answer:
        print(f"Transcribe: typing '{answer}'")
        type_and_submit(page, "#transcribeAnswerWord", "#transcribeSubmitBtn", answer)
        page.wait_for_timeout(400)
        return True

    skip_btn = page.locator("#transcribeSkipBtn")
    if skip_btn.count() > 0 and skip_btn.is_visible():
        print("Transcribe: no in-page answer, clicking skip")
        safe_click(page, skip_btn)
        page.wait_for_timeout(350)
        return True
    return False

# 8. chooseSpelling (#chooseSpelling)
def handle_choose_spelling(page) -> bool:
    container = page.locator("#chooseSpelling")
    if container.count() == 0 or not container.is_visible():
        return False

    target_answer = get_in_page_answer(page)
    if not target_answer:
        q_elem = page.locator("#cs_word")
        if q_elem.count() > 0:
            target_answer = get_answer_auto_update(q_elem.inner_text().strip())

    choices = page.locator(".chooseSpellingAnswer")
    if choices.count() == 0:
        return False

    idx, matched = find_best_choice_index(page, ".chooseSpellingAnswer", target_answer, exact_case_first=True)
    if idx is not None:
        time.sleep(random.uniform(0.08, 0.2))
        safe_click(page, choices.nth(idx))
        print(f"ChooseSpelling answered: '{matched}'")
    else:
        time.sleep(random.uniform(0.08, 0.2))
        safe_click(page, choices.first)
        print("ChooseSpelling: clicked first choice for auto-learn")

    page.wait_for_timeout(400)
    return True

# 9. listenAndChoose (#listenAndChoose)
def handle_listen_and_choose(page) -> bool:
    container = page.locator("#listenAndChoose")
    if container.count() == 0 or not container.is_visible():
        return False

    target_answer = get_in_page_answer(page)
    choices = page.locator(".listenAndChooseAnswer")
    if target_answer and choices.count() > 0:
        idx, matched = find_best_choice_index(page, ".listenAndChooseAnswer", target_answer)
        if idx is not None:
            time.sleep(random.uniform(0.08, 0.2))
            safe_click(page, choices.nth(idx))
            print(f"ListenAndChoose answered: '{matched}'")
            page.wait_for_timeout(400)
            return True

    skip_btn = page.locator("#listenAndChooseSkipBtn")
    if skip_btn.count() > 0 and skip_btn.is_visible():
        print("ListenAndChoose: skipping audio to chooseWord")
        safe_click(page, skip_btn)
        page.wait_for_timeout(400)
        return True

    if choices.count() > 0 and choices.first.is_visible():
        safe_click(page, choices.first)
        page.wait_for_timeout(400)
        return True
    return False

# 10. chooseWord (#chooseWord)
def handle_choose_word(page) -> bool:
    container = page.locator("#chooseWord")
    if container.count() == 0 or not container.is_visible():
        return False

    answers = page.locator(".chooseWordAnswer")
    if answers.count() == 0:
        return False

    target_answer = get_in_page_answer(page)
    question_elem = page.locator("#ch_word")
    question_raw = question_elem.inner_text().strip() if question_elem.count() > 0 else ""

    if not target_answer and question_raw:
        target_answer = get_answer_auto_update(question_raw)

    idx, matched = find_best_choice_index(page, ".chooseWordAnswer", target_answer)
    if idx is not None:
        time.sleep(random.uniform(0.08, 0.2))
        safe_click(page, answers.nth(idx))
        print(f"ChooseWord: '{question_raw}' -> '{matched}'")
        page.wait_for_timeout(400)
        return True

    print(f"ChooseWord: no match for '{question_raw}' (target '{target_answer}'), guessing first choice")
    time.sleep(random.uniform(0.08, 0.2))
    safe_click(page, answers.first)
    page.wait_for_timeout(400)
    return True

# 11. pexeso (#pexeso)
def handle_pexeso(page) -> bool:
    container = page.locator("#pexeso")
    if container.count() == 0 or not container.is_visible():
        return False

    pair = page.evaluate("""() => {
        const words = Array.from(document.querySelectorAll('#pq_words .pexesoCardWrapper'));
        const trans = Array.from(document.querySelectorAll('#pa_words .pexesoCardWrapper'));
        for (const w of words) {
            const wid = w.getAttribute('w_id');
            const pid1 = w.getAttribute('pexeso_id');
            if (!wid || !pid1 || !w.querySelector('.pexesoCard')) continue;
            const match = trans.find(t => t.getAttribute('w_id') === wid && t.querySelector('.pexesoCard'));
            if (match) {
                return { pid1, pid2: match.getAttribute('pexeso_id'), wid };
            }
        }
        return null;
    }""")

    if not pair:
        return False

    # Double-click each card at its wrapper center within 500ms.
    # Using mouse.click at the wrapper center avoids Playwright waiting on the 3D .pfx-flip
    # CSS animation on .pexesoBack (which would otherwise exceed WocaBee's 500ms threshold).
    for pid in (pair["pid1"], pair["pid2"]):
        wrapper = page.locator(f".pexesoCardWrapper[pexeso_id='{pid}']")
        if wrapper.count() == 0:
            continue
        try:
            wrapper.scroll_into_view_if_needed(timeout=1000)
            box = wrapper.bounding_box()
            if box and box["width"] > 0 and box["height"] > 0:
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                page.mouse.click(cx, cy)
                time.sleep(0.08)
                page.mouse.click(cx, cy)
            else:
                page.evaluate("""(pid) => {
                    const w = document.querySelector('.pexesoCardWrapper[pexeso_id="' + pid + '"]');
                    if (!w) return;
                    const c = w.querySelector('.pexesoCard');
                    if (c) { c.click(); c.click(); }
                }""", pid)
        except Exception:
            page.evaluate("""(pid) => {
                const w = document.querySelector('.pexesoCardWrapper[pexeso_id="' + pid + '"]');
                if (!w) return;
                const c = w.querySelector('.pexesoCard');
                if (c) { c.click(); c.click(); }
            }""", pid)
        time.sleep(0.1)

    page.wait_for_timeout(450)
    return True

# 12. completeWord (#completeWord)
def handle_complete_word(page) -> bool:
    container = page.locator("#completeWord")
    if container.count() == 0 or not container.is_visible():
        return False

    submit_btn = page.locator("#completeWordSubmitBtn")
    if submit_btn.count() > 0 and submit_btn.is_visible():
        time.sleep(random.uniform(0.08, 0.18))
        safe_click(page, submit_btn)
        print("CompleteWord: clicked submit button")
        page.wait_for_timeout(400)
        return True

    target_answer = get_in_page_answer(page)
    if not target_answer:
        q_elem = page.locator("#completeWordQuestion")
        if q_elem.count() > 0:
            q_text = q_elem.inner_text().strip()
            target_answer = get_answer_auto_update(q_text) or get_answer_auto_update(q_text.rstrip("!?. "))

    step = page.evaluate("""(target) => {
        const ansElem = document.querySelector('#completeWordAnswer');
        if (!ansElem) return { action: 'none' };
        const curr = ansElem.textContent || '';

        // 1. Check for mismatch in already-placed letters (only rollback if #rollbackBtn is active)
        const rb = document.querySelector('#rollbackBtn');
        const canRollback = rb && rb.getAttribute('is_hidden') === '0' && getComputedStyle(rb).visibility !== 'hidden';
        if (target && canRollback) {
            const minLen = Math.min(curr.length, target.length);
            for (let i = 0; i < minLen; i++) {
                if (curr[i] !== '_' && curr[i].toLowerCase() !== target[i].toLowerCase()) {
                    return { action: 'rollback' };
                }
            }
        }

        // 2. Check if all slots are filled (wait for submit button fade-in)
        const missingIdx = curr.indexOf('_');
        if (missingIdx === -1) {
            return { action: 'wait_submit' };
        }

        // 3. Find available (NOT visibility:hidden and NOT is_hidden="1") character tiles
        const chars = Array.from(document.querySelectorAll('#characters .char')).filter(b =>
            b.getAttribute('is_hidden') !== '1' &&
            getComputedStyle(b).visibility !== 'hidden' &&
            b.offsetParent !== null
        );
        if (chars.length === 0) return { action: 'none' };

        if (target && missingIdx < target.length) {
            const needed = target[missingIdx];
            // Priority 1: exact case match (e.g. German uppercase 'G' vs 'g')
            let match = chars.find(b => b.textContent === needed);
            // Priority 2: case-insensitive match
            if (!match) match = chars.find(b => b.textContent.toLowerCase() === needed.toLowerCase());
            // Priority 3: accent-stripped match
            if (!match) {
                const normNeeded = needed.normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').toLowerCase();
                match = chars.find(b => b.textContent.normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').toLowerCase() === normNeeded);
            }
            if (match) {
                return { action: 'click_char', index: match.getAttribute('index'), char: match.textContent, slot: missingIdx };
            }
        }

        // Fallback: first available char
        return { action: 'fallback_char', index: chars[0].getAttribute('index'), char: chars[0].textContent };
    }""", target_answer or "")

    act = step.get("action") if isinstance(step, dict) else None
    if act == "rollback":
        print("CompleteWord: rolling back mismatched character")
        safe_click(page, page.locator("#rollbackBtn"))
        page.wait_for_timeout(200)
        return True
    elif act == "wait_submit":
        page.wait_for_timeout(300)
        if submit_btn.count() > 0 and submit_btn.is_visible():
            safe_click(page, submit_btn)
            print("CompleteWord: clicked submit button")
            page.wait_for_timeout(400)
        return True
    elif act in ("click_char", "fallback_char"):
        idx = step.get("index")
        tile = page.locator(f"#characters .char[index='{idx}']")
        if tile.count() > 0:
            safe_click(page, tile.first)
            print(f"CompleteWord: {act} '{step.get('char')}'")
            page.wait_for_timeout(180)
            return True

    return False

# 13. oneOutOfMany (#oneOutOfMany)
def handle_one_out_of_many(page) -> bool:
    container = page.locator("#oneOutOfMany")
    if container.count() == 0 or not container.is_visible():
        return False

    q_elem = page.locator("#oneOutOfManyQuestionWord")
    q_raw = q_elem.inner_text().strip() if q_elem.count() > 0 else ""

    target_answer = get_in_page_answer(page)
    if not target_answer and q_raw:
        target_answer = get_answer_auto_update(q_raw)

    choices = page.locator(".oneOutOfManyWord")
    if choices.count() == 0:
        return False

    # Search across ALL choices (even those still fading in over 100..3000ms!)
    idx, matched = find_best_choice_index(page, ".oneOutOfManyWord", target_answer, include_hidden=True)
    if idx is not None:
        target_btn = choices.nth(idx)
        # Wait up to 3.5s for this choice's fadeIn animation to make it visible
        waited = 0.0
        while waited < 3.5 and container.is_visible() and not target_btn.is_visible():
            time.sleep(0.1)
            waited += 0.1
        if target_btn.is_visible():
            time.sleep(random.uniform(0.05, 0.15))
            safe_click(page, target_btn)
            print(f"OneOutOfMany answered: '{matched}'")
            page.wait_for_timeout(400)
            return True

    # Fallback: click first visible choice if no match exists
    for i in range(choices.count()):
        if choices.nth(i).is_visible():
            safe_click(page, choices.nth(i))
            print(f"OneOutOfMany: guessed first visible choice for '{q_raw}'")
            page.wait_for_timeout(400)
            return True

    page.wait_for_timeout(200)
    return True

# 14. matchPair (#matchPair)
def handle_match_pair(page) -> bool:
    container = page.locator("#matchPair")
    if container.count() == 0 or not container.is_visible():
        return False

    pair = page.evaluate("""() => {
        const words = Array.from(document.querySelectorAll('.matchPairWord:not(.disabled)'));
        const trans = Array.from(document.querySelectorAll('.matchPairTranslation:not(.disabled)'));
        if (words.length === 0 || trans.length === 0) return { state: 'done' };

        for (const w of words) {
            if (w.offsetParent === null) continue;
            const wid = w.getAttribute('w_id');
            if (!wid) continue;
            const match = trans.find(t => t.offsetParent !== null && t.getAttribute('w_id') === wid);
            if (match) {
                return { state: 'pair', wid, wText: w.textContent.trim(), tText: match.textContent.trim() };
            }
        }
        return { state: 'fading' };
    }""")

    if not pair or pair.get("state") == "done":
        page.wait_for_timeout(300)
        return True

    if pair.get("state") == "fading":
        page.wait_for_timeout(250)
        return True

    wid = pair["wid"]
    w_btn = page.locator(f".matchPairWord:not(.disabled)[w_id='{wid}']").first
    t_btn = page.locator(f".matchPairTranslation:not(.disabled)[w_id='{wid}']").first
    if w_btn.count() > 0 and t_btn.count() > 0:
        safe_click(page, w_btn)
        time.sleep(0.08)
        safe_click(page, t_btn)
        print(f"MatchPair: '{pair['wText']}' <-> '{pair['tText']}'")
        page.wait_for_timeout(250)
        return True

    return False

# 15. findPair (#findPair)
def handle_find_pair(page) -> bool:
    container = page.locator("#findPair")
    if container.count() == 0 or not container.is_visible():
        return False

    pair = page.evaluate("""() => {
        const qButtons = Array.from(document.querySelectorAll('.fp_q'));
        const aButtons = Array.from(document.querySelectorAll('.fp_a'));
        if (qButtons.length === 0 || aButtons.length === 0) return null;

        const targetWidEl = document.querySelector('#word_id');
        const targetWid = targetWidEl ? targetWidEl.value.trim() : null;

        if (targetWid) {
            const q = qButtons.find(b => b.getAttribute('w_id') === targetWid && b.offsetParent !== null);
            const a = aButtons.find(b => b.getAttribute('w_id') === targetWid && b.offsetParent !== null);
            if (q && a) {
                return {
                    wid: targetWid,
                    qText: q.innerText.trim(),
                    aText: a.innerText.trim(),
                    qDisabled: !!q.disabled,
                    aDisabled: !!a.disabled
                };
            }
        }

        for (const q of qButtons) {
            if (q.offsetParent === null) continue;
            const wid = q.getAttribute('w_id');
            if (!wid) continue;
            const a = aButtons.find(b => b.getAttribute('w_id') === wid && b.offsetParent !== null);
            if (a) {
                return {
                    wid,
                    qText: q.innerText.trim(),
                    aText: a.innerText.trim(),
                    qDisabled: !!q.disabled,
                    aDisabled: !!a.disabled
                };
            }
        }
        return null;
    }""")

    if pair:
        wid = pair["wid"]
        q_loc = page.locator(f".fp_q[w_id='{wid}']").first
        a_loc = page.locator(f".fp_a[w_id='{wid}']").first
        if not pair["qDisabled"] and q_loc.count() > 0:
            safe_click(page, q_loc)
            time.sleep(0.08)
        if not pair["aDisabled"] and a_loc.count() > 0:
            safe_click(page, a_loc)

        q_txt = pair["qText"]
        a_txt = pair["aText"]
        print(f"FindPair matched (w_id={wid}): '{q_txt}' <-> '{a_txt}'")
        if q_txt and a_txt:
            norm_q = normalize(q_txt)
            norm_a = normalize(a_txt)
            if norm_q and norm_a and WORD_TABLE.get(norm_q) != norm_a:
                WORD_TABLE[norm_q] = norm_a
                save_word_table()
        page.wait_for_timeout(400)
        return True

    # Fallback: click first enabled Q and A to avoid freezing
    q_first = page.locator("#q_words .fp_q:not([disabled])").first
    a_first = page.locator("#a_words .fp_a:not([disabled])").first
    if q_first.count() > 0 and a_first.count() > 0:
        safe_click(page, q_first)
        time.sleep(0.1)
        safe_click(page, a_first)
        print("FindPair: clicked fallback pair")
        page.wait_for_timeout(400)
        return True

    return False

# ---------------- CLASS PAGE NAVIGATION ----------------

def dismiss_modals(page):
    selectors = [
        "#wocacupModal .close",
        "#wocacupModal button[data-dismiss='modal']",
        ".modal.show .btn-close",
        ".modal.show .close",
        ".modal.show button:has-text('Zavřít')",
        ".modal.show button:has-text('Zavrieť')",
        ".modal.show button:has-text('Rozumím')",
        ".swal2-confirm",
        "button:has-text('Rozumím')",
        "button:has-text('Souhlasím')",
        "button:has-text('Zavřít')",
        "button:has-text('Zavrieť')",
        "button:has-text('Close')",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                safe_click(page, loc.first, timeout=1000)
                page.wait_for_timeout(300)
        except Exception:
            pass

def click_next_required_package(page):
    """
    Waits for the class package list to load, then finds and launches the next
    unlocked required (homework/todo) package card.
    Returns { name: str, remaining: int, href: str } or None if 0 remain.
    """
    dismiss_modals(page)
    try:
        page.wait_for_selector("#packagesTable, .pkgCard, .pTableRow", timeout=10000)
    except Exception:
        return None
    dismiss_modals(page)

    try:
        pkg_info = page.evaluate("""() => {
            const cards = Array.from(document.querySelectorAll('.pkgCard[data-status="todo"], .pTableRow[data-status="todo"]'));
            const unlocked = cards.filter(c => {
                const pkgSpan = c.querySelector('.package');
                return !pkgSpan || pkgSpan.getAttribute('is_locked') !== '1';
            });
            if (unlocked.length === 0) return null;

            const target = unlocked.find(c => c.classList.contains('pkgCard--next')) || unlocked[0];
            const nameElem = target.querySelector('.pkgCard-name, .package-name');
            const name = nameElem ? nameElem.innerText.trim() : 'Unknown';

            const btn = target.querySelector('.pkgBtn--run, .pkgBtn, a[id^="btnRun"], a.pkgBtn') || target.querySelector('a');
            if (!btn) return null;

            const href = btn.getAttribute('href') || '';
            const visible = btn.offsetParent !== null;
            if (visible) {
                btn.click();
            }
            return { name, remaining: unlocked.length, href, clicked: visible };
        }""")

        if pkg_info and not pkg_info.get("clicked") and pkg_info.get("href"):
            # Card was on another pagination page or tab — navigate directly to its href
            page.goto(pkg_info["href"])
        return pkg_info
    except Exception:
        return None

def enable_double_points_if_configured(page):
    if not DOUBLE_POINTS:
        return
    try:
        wrapper = page.locator("#toggleWrapper")
        toggle = page.locator("#levelToggle")
        if wrapper.count() > 0 and toggle.count() > 0 and not toggle.is_checked():
            safe_click(page, page.locator("#toggleWrapper .slider"))
            print("Double points enabled.")
    except Exception:
        pass

# ------------- Main Loop -------------

EXERCISE_HANDLERS = [
    handle_choose_picture,
    handle_describe_picture,
    handle_add_missing_word,
    handle_arrange_words,
    handle_complete_word,
    handle_translate_word,
    handle_translate_falling_word,
    handle_choose_word,
    handle_listen_and_choose,
    handle_choose_spelling,
    handle_find_pair,
    handle_pexeso,
    handle_one_out_of_many,
    handle_match_pair,
    handle_transcribe,
]

def main():
    notify_ntfy("Wocabee Bot Started", "The bot has been started and is connecting into the browser.")
    StopBot = False

    with sync_playwright() as p:
        try:
            try:
                browser = p.chromium.connect_over_cdp(DEBUG_PORT)
                context = browser.contexts[0]
                page = find_target_page(context)
                if not page:
                    raise RuntimeError(f"No open tab found containing '{URLBASE}'")
                print("Attached to tab:", page.url)
            except Exception as e:
                print("CDP connection failed (", e, "), launching new browser instance")
                browser = p.chromium.launch(
                    headless=HEADLESS,
                    args=["--no-sandbox", "--remote-debugging-port=9222"]
                )
                context = browser.new_context()
                page = context.new_page()
                if URLBASE:
                    page.goto(URLBASE)
                print("Opened new browser, current URL:", page.url)

            extract_words_from_page(page)
            pts = get_current_points(page)
            original_points = pts if pts is not None else 0
            points = original_points
            last_milestone = original_points

            while True:
                try:
                    # 1. Check if we are on the class overview page (not inside /practice/)
                    if "/class/" in page.url and "/practice/" not in page.url:
                        if COMPLETE_ALL_REQUIRED:
                            print("On class page — checking for remaining required packages...")
                            page.wait_for_timeout(1200)
                            next_pkg = click_next_required_package(page)
                            if next_pkg:
                                pkg_name = next_pkg.get("name", "Unknown")
                                rem = next_pkg.get("remaining", 1)
                                print("\n=======================================================")
                                print(f"[+] Starting next required package: '{pkg_name}' ({rem} to do)")
                                print("=======================================================\n")
                                notify_ntfy("Wocabee Bot Next Package", f"Starting '{pkg_name}' ({rem} to do)")
                                try:
                                    page.wait_for_url("**/practice/**", timeout=15000)
                                except Exception:
                                    pass
                                page.wait_for_timeout(1000)
                                enable_double_points_if_configured(page)
                                extract_words_from_page(page)
                                pts = get_current_points(page)
                                original_points = pts if pts is not None else 0
                                points = original_points
                                last_milestone = original_points
                                continue
                            else:
                                print("\n=======================================================")
                                print("[SUCCESS] All required homework packages have been completed!")
                                print("=======================================================\n")
                                notify_ntfy("Wocabee Bot Complete", "All required homework packages have been completed!")
                                exit(0)
                        else:
                            print("Returned to class overview — exercise session complete.")
                            notify_ntfy("Wocabee Bot Finished", "Returned to class overview.")
                            exit(0)

                    # 2. Keep vocabulary & pictures up to date from page
                    extract_words_from_page(page)

                    # 3. Handle transient overlays (saving modal, correct/checkpoint animations, quit screen)
                    if handle_transient_screens(page):
                        continue

                    # 4. Handle intro cards, problem-words review, and continue buttons
                    if handle_intro_and_continue(page):
                        continue

                    # 5. Check if current stage/homework finished & saved -> click "Uložit a odejít"
                    if check_stage_or_homework_finished(page):
                        continue

                    # 6. Handle incorrect feedback — auto-learn and advance
                    if handle_incorrect_autolearn(page):
                        continue

                    # 7. Run all 15 exercise handlers
                    handled = False
                    for handler in EXERCISE_HANDLERS:
                        if handler(page):
                            handled = True
                            break
                    if handled:
                        continue

                    # 8. Points / milestone tracking (optional mode when COMPLETE_ALL_REQUIRED is false)
                    pts = get_current_points(page)
                    if pts is not None:
                        points = pts
                        if milestone_reminder > 0:
                            while points >= last_milestone + milestone_reminder:
                                last_milestone += milestone_reminder
                                print(f"Milestone reached: {points} points (original: {original_points})")
                                notify_ntfy("Wocabee Bot Progress", f"Current points: {points}")

                        if not COMPLETE_ALL_REQUIRED and addon_points != -1 and points >= original_points + addon_points:
                            print(f"Target reached: {points} points (original: {original_points}, addon: {addon_points}), stopping bot")
                            notify_ntfy("Wocabee Bot Target Reached", f"Reached {points} points, stopping.")
                            StopBot = True

                    if StopBot:
                        page.wait_for_selector("#backBtn", timeout=5000)
                        safe_click(page, page.locator("#backBtn"))
                        try:
                            page.wait_for_selector("#standardView, #packagesTable", state="visible", timeout=15000)
                        except Exception:
                            pass
                        break

                    time.sleep(0.1)

                except Exception as e_inner:
                    tb = traceback.format_exc()
                    print("Runtime error:", tb)
                    notify_ntfy("Wocabee Bot Runtime Error", tb)
                    time.sleep(1)

        except Exception as e_outer:
            tb = traceback.format_exc()
            print("Startup/Connection error:", tb)
            notify_ntfy("Wocabee Bot Startup Error", tb)

if __name__ == "__main__":
    main()

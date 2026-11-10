"""Fill application forms with Playwright, then submit (auto or after your review).

Works on any ATS form generically: fields are discovered from the DOM with their labels and
options, Claude decides each value, and Playwright fills them. Greenhouse, Lever and Ashby
hosted forms are what the sources produce, so those are the tested path.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, TimeoutError as PWTimeout, sync_playwright
from rich.console import Console
from rich.table import Table

from . import db, llm
from .config import BROWSER_DIR, Config

console = Console()

# Tags every visible field with data-jobbot="fN" and returns a description of each.
# Radio buttons and checkboxes sharing a name are collapsed into one field with options.
EXTRACT_JS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 400);
  // Text of a node without the text of form controls inside it (e.g. a <label> wrapping a <select>).
  const own = n => {
    if (!n.querySelector('select, textarea')) return clean(n.innerText);  // rendered text only
    const c = n.cloneNode(true);
    c.querySelectorAll('select, option, input, textarea, button, svg').forEach(x => x.remove());
    return clean(c.textContent);
  };
  const weak = t => !t || t.replace(/[✱*\s]/g, '').length < 2 ||
    /^(attach|upload|select\.*|choose file|browse|drop files here)$/i.test(t.replace(/[✱*]/g, '').trim());
  const visible = el => {
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && (r.width > 0 || r.height > 0);
  };
  const byFor = el => el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
  // Closest label-like text that comes before el, searching outwards through its ancestors.
  const nearestLabel = el => {
    for (let p = el.parentElement, i = 0; p && i < 6; p = p.parentElement, i++) {
      const cands = [...p.querySelectorAll('label, legend, h3, h4, p, span, div')].filter(x =>
        !x.contains(el) && !x.querySelector('input, select, textarea') &&
        (x.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING));
      for (const c of cands.reverse()) { const t = own(c); if (!weak(t)) return t; }
    }
    return '';
  };
  const labelOf = el => {
    const tries = [
      () => byFor(el) && own(byFor(el)),
      () => el.getAttribute('aria-labelledby') && clean(el.getAttribute('aria-labelledby').split(/\s+/)
              .map(i => document.getElementById(i)?.innerText || '').join(' ')),
      () => clean(el.getAttribute('aria-label')),
      () => el.closest('label') && own(el.closest('label')),
      () => nearestLabel(el),
    ];
    for (const t of tries) { const v = t(); if (!weak(v)) return v; }
    return clean(el.placeholder || el.name || el.id);
  };
  const groupQuestion = el => {
    const g = el.closest('fieldset, [role=group], [role=radiogroup]');
    const t = g && (g.querySelector('legend') ? own(g.querySelector('legend')) : clean(g.getAttribute('aria-label')));
    return weak(t) ? (nearestLabel(el.closest('label') || el) || clean(el.name)) : t;
  };
  const out = [], groups = {};
  let n = 0;
  for (const el of document.querySelectorAll('input, textarea, select')) {
    const type = el.tagName === 'SELECT' ? 'select' : el.tagName === 'TEXTAREA' ? 'textarea' : (el.type || 'text').toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'search'].includes(type)) continue;
    const choice = type === 'radio' || type === 'checkbox';
    if (type !== 'file') {
      // Custom-styled radios/checkboxes are often invisible themselves; their label is what shows.
      if (!visible(choice ? (el.closest('label') || el) : el)) continue;
      // Hidden mirror inputs (e.g. react-select's required-value input) duplicate a combobox.
      if (!choice && (el.getAttribute('aria-hidden') === 'true' || getComputedStyle(el).opacity === '0') &&
          el.getAttribute('role') !== 'combobox') continue;
    }
    const id = 'f' + (n++);
    el.setAttribute('data-jobbot', id);
    if (choice) {
      const key = type + ':' + (el.name || id);
      const opt = clean((byFor(el) && own(byFor(el))) || (el.closest('label') && own(el.closest('label'))) ||
                        el.getAttribute('aria-label') || el.value);
      if (!groups[key]) {
        groups[key] = { id, type, label: groupQuestion(el), required: el.required, options: [], option_ids: [] };
        out.push(groups[key]);
      }
      groups[key].options.push(opt);
      groups[key].option_ids.push(id);
      groups[key].required ||= el.required;
      continue;
    }
    const f = { id, type, label: labelOf(el), name: el.name || el.id || '',
                required: el.required || el.getAttribute('aria-required') === 'true' };
    if (el.getAttribute('role') === 'combobox') f.type = 'combobox';
    if (type === 'select') f.options = [...el.options].map(o => clean(o.text)).filter(Boolean);
    if (type === 'file') f.accept = el.accept || '';
    if (el.value && type !== 'file') f.current_value = clean(el.value);
    out.push(f);
  }
  return out;
}
"""

CAPTCHA_SELECTORS = "iframe[src*='recaptcha'], iframe[src*='hcaptcha'], iframe[src*='turnstile']"
SUBMIT_SELECTORS = [
    "button:has-text('Submit application')", "button:has-text('Submit Application')",
    "button[type=submit]", "input[type=submit]", "button:has-text('Submit')",
]
SUCCESS_RE = re.compile(r"thank you for applying|application (has been )?(received|submitted)|thanks for applying|we.ve received your application", re.I)


@dataclass
class FillResult:
    filled: int = 0
    needs_human: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    captcha: bool = False


def open_browser(p, cfg: Config) -> BrowserContext:
    # Persistent profile: any site logins you do by hand are remembered between runs.
    return p.chromium.launch_persistent_context(
        str(BROWSER_DIR), headless=bool(cfg.apply.get("headless", False)),
        viewport={"width": 1280, "height": 900}, accept_downloads=False,
    )


def open_form(page: Page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_selector("input[type=file], form input, form textarea", timeout=20_000)
    except PWTimeout:
        pass
    # Some boards show the description first with an "Apply" button.
    if not page.query_selector("input[type=file]"):
        btn = page.query_selector("a:has-text('Apply'), button:has-text('Apply')")
        if btn:
            btn.click()
            page.wait_for_timeout(2500)
    page.wait_for_timeout(1500)  # let React forms hydrate


def is_cover_field(f: dict) -> bool:
    return "cover" in f["label"].lower() or "cover" in f.get("name", "").lower()


def captcha_visible(page: Page) -> bool:
    """A CAPTCHA the user must solve, not the invisible badge most ATSs load on every form."""
    for frame in page.query_selector_all(CAPTCHA_SELECTORS):
        src = frame.get_attribute("src") or ""
        box = frame.bounding_box()
        if "size=invisible" in src or not frame.is_visible() or not box:
            continue
        if box["height"] > 40 and box["width"] > 100:
            return True
    return False


def listbox_options(page: Page, box):
    """Options of the menu this combobox opened (not some other dropdown left open)."""
    owner = box.get_attribute("aria-controls") or box.get_attribute("aria-owns")
    return page.locator(f"[id='{owner}'] [role=option]") if owner else page.locator("[role=option]:visible")


def close_combobox(page: Page, box) -> None:
    page.keyboard.press("Escape")
    box.evaluate("el => el.blur()")


def read_combobox_options(page: Page, fields: list[dict]) -> None:
    """Open each custom dropdown (react-select etc.) once to list its options for Claude."""
    for f in fields:
        if f["type"] != "combobox":
            continue
        try:
            box = page.locator(f"[data-jobbot='{f['id']}']")
            box.click(timeout=2000)
            page.wait_for_timeout(400)
            opts = listbox_options(page, box).all_inner_texts()[:80]
            close_combobox(page, box)
            if opts:
                f["options"] = [o.strip() for o in opts if o.strip()]
        except Exception:  # noqa: BLE001 - options are a hint only
            pass


def upload_files(page: Page, fields: list[dict], resume: Path | None, cover: Path | None, result: FillResult) -> None:
    for f in fields:
        if f["type"] != "file":
            continue
        path = cover if is_cover_field(f) else resume
        if not path or not path.exists():
            if f.get("required"):
                result.needs_human.append(f"{f['label']}: no file to upload")
            continue
        try:
            page.set_input_files(f"[data-jobbot='{f['id']}']", str(path))
            result.filled += 1
            page.wait_for_timeout(1200)  # some ATSs parse the resume and prefill fields
        except Exception as e:  # noqa: BLE001 - surface any Playwright failure to the user
            result.failed.append(f"{f['label']}: upload failed ({e.__class__.__name__})")


def best_option(options: list[str], value: str) -> int | None:
    v = value.strip().lower()
    for i, o in enumerate(options):
        if o.strip().lower() == v:
            return i
    for i, o in enumerate(options):
        if v and (v in o.lower() or o.lower() in v):
            return i
    return None


def fill_field(page: Page, f: dict, value: str) -> None:
    sel = f"[data-jobbot='{f['id']}']"
    kind = f["type"]
    if kind in ("radio", "checkbox"):
        wanted = [v for v in (x.strip() for x in value.split("|")) if v]
        for w in wanted:
            i = best_option(f["options"], w)
            if i is None:
                raise ValueError(f"option {w!r} not found")
            box = page.locator(f"[data-jobbot='{f['option_ids'][i]}']")
            try:
                box.check(timeout=3000)
            except Exception:  # noqa: BLE001 - custom-styled inputs: click the input or its label
                box.click(force=True)
    elif kind == "select":
        i = best_option(f.get("options", []), value)
        if i is None:
            raise ValueError(f"option {value!r} not found")
        page.select_option(sel, label=f["options"][i])
    elif kind == "combobox":
        box = page.locator(sel)
        box.click()
        box.fill(value)
        page.wait_for_timeout(900)  # async options (cities, schools) load after typing
        options = listbox_options(page, box)
        texts = options.all_inner_texts()
        i = best_option(texts, value) if texts else None
        if i is not None:
            options.nth(i).click()
        else:
            page.keyboard.press("Enter")
    else:
        page.fill(sel, value)


def fill_form(page: Page, cfg: Config, job, resume: Path | None, cover: Path | None) -> FillResult:
    result = FillResult()
    fields = page.evaluate(EXTRACT_JS)
    if not fields:
        result.failed.append("no form fields found on page")
        return result
    upload_files(page, fields, resume, cover, result)
    # Re-scan after upload: some ATSs autofill from the parsed resume or reveal new fields.
    fields = [f for f in page.evaluate(EXTRACT_JS) if f["type"] != "file"]
    read_combobox_options(page, fields)
    cover_md = cover.with_suffix(".md").read_text() if cover and cover.with_suffix(".md").exists() else None
    llm_fields = [{k: v for k, v in f.items() if k != "option_ids"} for f in fields]
    plan = llm.plan_form(cfg, job, llm_fields, cover_md)
    by_id = {f["id"]: f for f in fields}

    table = Table("Field", "Value", "", show_lines=False, title=f"{job['company']} — {job['title']}")
    for a in plan.answers:
        f = by_id.get(a.field_id)
        if not f:
            continue
        flag = "[yellow]CHECK[/]" if a.needs_human else ""
        if a.needs_human:
            result.needs_human.append(f"{f['label']}: {a.note}")
        if a.value:
            try:
                fill_field(page, f, a.value)
                result.filled += 1
            except Exception as e:  # noqa: BLE001
                flag = "[red]FAILED[/]"
                result.failed.append(f"{f['label']}: {e}")
        elif f.get("required") and not f.get("current_value"):
            flag = "[yellow]REQUIRED, EMPTY[/]"
            result.needs_human.append(f"{f['label']}: required but left empty")
        table.add_row(f["label"][:60], a.value[:70].replace("\n", " "), flag)
    console.print(table)
    result.captcha = captcha_visible(page)
    return result


def click_submit(page: Page) -> bool:
    for s in SUBMIT_SELECTORS:
        btn = page.query_selector(s)
        if btn and btn.is_visible():
            btn.click()
            return True
    return False


def looks_submitted(page: Page, start_url: str, wait_s: float = 12) -> bool:
    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            if SUCCESS_RE.search(page.inner_text("body")) or ("confirmation" in page.url and page.url != start_url):
                return True
        except Exception:  # noqa: BLE001 - page mid-navigation
            pass
        page.wait_for_timeout(1000)
    return False


def ask(prompt: str, choices: str) -> str:
    while True:
        a = console.input(prompt).strip().lower()[:1]
        if a in choices:
            return a


def apply_one(ctx: BrowserContext, cfg: Config, conn, job, auto_submit: bool) -> str:
    """Returns 'applied', 'skipped', 'later' or 'quit'."""
    resume = Path(job["resume_path"]) if job["resume_path"] else None
    cover = Path(job["cover_path"]) if job["cover_path"] else None
    page = ctx.new_page()
    try:
        console.rule(f"[bold]{job['company']} — {job['title']}[/]  (score {job['score']})")
        console.print(job["apply_url"])
        open_form(page, job["apply_url"])
        result = fill_form(page, cfg, job, resume, cover)
        for msg in result.needs_human:
            console.print(f"  [yellow]needs you:[/] {msg}")
        for msg in result.failed:
            console.print(f"  [red]failed:[/] {msg}")
        if result.captcha:
            console.print("  [yellow]CAPTCHA present: solve it in the browser window.[/]")

        clean = not (result.needs_human or result.failed or result.captcha)
        if auto_submit and clean:
            console.print("  Auto-submitting...")
            start = page.url
            if click_submit(page) and looks_submitted(page, start):
                db.update(conn, job["id"], "applied", applied_at=db.now(), note="auto-submitted")
                console.print("  [green]Submitted.[/]")
                return "applied"
            console.print("  [yellow]Could not confirm submission; falling back to manual review.[/]")

        console.print("\n  Review the form in the browser window and fix anything flagged.")
        choice = ask(
            r"  [bold]\[s][/]ubmit for me · [bold]\[d][/]one, I submitted it · [bold]\[k][/] skip job · "
            r"[bold]\[l][/]ater · [bold]\[q][/]uit > ", "sdklq")
        if choice == "s":
            start = page.url
            if click_submit(page) and looks_submitted(page, start):
                db.update(conn, job["id"], "applied", applied_at=db.now())
                console.print("  [green]Submitted.[/]")
                return "applied"
            choice = ask("  Couldn't confirm. Did it go through? [y/n] > ", "yn")
            choice = "d" if choice == "y" else "l"
        if choice == "d":
            db.update(conn, job["id"], "applied", applied_at=db.now())
            return "applied"
        if choice == "k":
            db.update(conn, job["id"], "skipped", note="skipped at apply step")
            return "skipped"
        return "quit" if choice == "q" else "later"
    except Exception as e:  # noqa: BLE001 - one bad form must not stop the batch
        console.print(f"  [red]Error: {e}[/]")
        db.update(conn, job["id"], "error", note=str(e)[:500])
        return "later"
    finally:
        page.close()


def apply_jobs(cfg: Config, conn, jobs, auto_submit: bool) -> None:
    limit = int(cfg.apply.get("daily_limit", 15))
    with sync_playwright() as p:
        ctx = open_browser(p, cfg)
        try:
            for job in jobs:
                done = db.applied_today(conn)
                if done >= limit:
                    console.print(f"[yellow]Daily limit of {limit} applications reached.[/]")
                    break
                if apply_one(ctx, cfg, conn, job, auto_submit) == "quit":
                    break
        finally:
            ctx.close()

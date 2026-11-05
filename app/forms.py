"""Reads and fills application forms in any frame of a Playwright page."""
import re
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

SCAN_JS = r"""
() => {
  window.__apN = window.__apN || 0;
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => {
    if (!el) return false;
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const mark = el => el.dataset.apId || (el.dataset.apId = 'f' + (++window.__apN));
  const byIds = ids => clean((ids || '').split(/\s+/).map(i => document.getElementById(i)?.innerText || '').join(' '));
  // A label wrapping a <select> would otherwise read out every option as part of its text.
  const labelText = label => {
    const copy = label.cloneNode(true);
    copy.querySelectorAll('select, textarea').forEach(n => n.remove());
    return clean(copy.textContent);
  };
  const ownLabel = el => {
    const aria = byIds(el.getAttribute('aria-labelledby')) || el.getAttribute('aria-label');
    if (aria) return clean(aria);
    if (el.labels && el.labels.length) return labelText(el.labels[0]);
    const wrap = el.closest('label');
    return wrap ? labelText(wrap) : '';
  };
  const nearby = el => {
    const box = el.closest('.field, .form-group, .form-field, [class*="field"], [class*="question"], div, li, td');
    const head = box?.querySelector('label, legend, .label, [class*="label"]');
    return clean(head?.innerText);
  };
  const groupLabel = el => {
    const set = el.closest('fieldset');
    const legend = set?.querySelector('legend');
    if (legend) return clean(legend.innerText);
    const group = el.closest('[role="radiogroup"], [role="group"]');
    if (group) return clean(byIds(group.getAttribute('aria-labelledby')) || group.getAttribute('aria-label'));
    return nearby(el.parentElement?.parentElement || el);
  };
  const skipped = el => {
    if (el.closest('nav, header, footer, [role="search"], [role="navigation"]')) return true;
    const hint = (el.name + ' ' + el.id + ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.placeholder || '')).toLowerCase();
    return el.type === 'search' || /\bsearch\b|keyword/.test(hint);
  };
  const isRequired = (el, label) => el.required || el.getAttribute('aria-required') === 'true' ||
    /\*\s*$/.test(label) || /\(required\)/i.test(label);
  const isInvalid = el => el.getAttribute('aria-invalid') === 'true' ||
    !!el.closest('.error, .has-error, .invalid, .is-invalid, [class*="field-error"], [data-invalid="true"]');
  const tidy = label => clean(label.replace(/\*\s*$/, '').replace(/\(required\)/i, ''));

  const fields = [], groups = {};
  for (const el of document.querySelectorAll('input, select, textarea, button[aria-haspopup="listbox"]')) {
    if (el.tagName === 'BUTTON') {
      // Workday-style dropdown: its options only exist once it is opened, so load_options reads them.
      if (!visible(el) || el.disabled || el.closest('nav, header, footer')) continue;
      // Without a label, Workday reads its question out as "<question> <current value> Required".
      const shown = clean(el.innerText);
      const aria = clean(el.getAttribute('aria-label'));
      const ariaRequired = /\bRequired$/.test(aria);
      const ariaQuestion = aria.replace(/\s*Required$/, '').replace(shown, '').trim();
      const label = (el.labels?.length ? labelText(el.labels[0]) : '') || ariaQuestion || nearby(el);
      fields.push({ id: mark(el), kind: 'select', listbox: true, label: tidy(label), required: /\*\s*$/.test(label) || ariaRequired,
        invalid: isInvalid(el), options: [], value: /^(select|choose)\b/i.test(shown) ? '' : shown });
      continue;
    }
    const type = (el.type || 'text').toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image'].includes(type) || el.disabled || el.readOnly) continue;
    if (skipped(el)) continue;
    if (type === 'radio' || type === 'checkbox') {
      const label = ownLabel(el);
      if (!visible(el) && !visible(el.labels?.[0])) continue;
      const peers = el.name ? document.querySelectorAll(`input[type="${type}"][name="${CSS.escape(el.name)}"]`).length : 1;
      if (type === 'checkbox' && peers < 2) {
        fields.push({ id: mark(el), kind: 'checkbox', label: tidy(label || nearby(el)),
          required: isRequired(el, label), invalid: isInvalid(el), value: el.checked ? 'Yes' : '', options: [] });
        continue;
      }
      const key = type + ':' + (el.name || mark(el));
      let g = groups[key];
      if (!g) {
        const title = groupLabel(el);
        g = groups[key] = { id: key, kind: type === 'radio' ? 'radio' : 'checkbox_group', label: tidy(title),
          required: isRequired(el, title), invalid: false, value: '', options: [] };
        fields.push(g);
      }
      g.required = g.required || el.required;
      g.invalid = g.invalid || isInvalid(el);
      g.options.push({ label: label || el.value, value: el.value, id: mark(el) });
      if (el.checked) g.value = g.value ? g.value + ', ' + label : label;
      continue;
    }
    if (type !== 'file' && !visible(el)) continue;
    const label = ownLabel(el) || el.placeholder || nearby(el) || el.name || '';
    const base = { id: mark(el), label: tidy(label), required: isRequired(el, label),
      invalid: isInvalid(el) };
    if (el.tagName === 'SELECT') {
      const options = [...el.options].filter(o => o.value !== '' && !/^(select|choose|please|--)/i.test(clean(o.text)))
        .map(o => ({ label: clean(o.text), value: o.value }));
      const chosen = el.selectedOptions[0];
      fields.push({ ...base, kind: 'select', options,
        value: chosen && options.some(o => o.value === chosen.value) ? clean(chosen.text) : '' });
    } else if (type === 'file') {
      fields.push({ ...base, kind: 'file', options: [], multiple: el.multiple, value: el.files?.length ? el.files[0].name : '' });
    } else {
      // Workday's search box keeps its chosen value in a list beside it, not in the box itself.
      const prompt = el.dataset.uxiWidgetType === 'selectinput';
      const combo = el.getAttribute('role') === 'combobox' || prompt;
      const chosen = prompt ? clean(el.closest('[data-automation-id="multiSelectContainer"]')
        ?.querySelector('[data-automation-id="selectedItemList"]')?.innerText) : '';
      fields.push({ ...base, kind: el.tagName === 'TEXTAREA' ? 'textarea' : combo ? 'combobox' : type, prompt,
        options: [], value: el.value || chosen, max_length: el.maxLength > 0 ? el.maxLength : null,
        one_time_code: el.autocomplete === 'one-time-code' });
    }
  }
  return fields;
}
"""

ACTIONS_JS = r"""
() => {
  const out = [];
  for (const el of document.querySelectorAll('button, input[type="submit"], input[type="button"], [role="button"], a')) {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    if (r.width <= 0 || r.height <= 0 || s.visibility === 'hidden' || s.display === 'none' || el.disabled) continue;
    const text = (el.innerText || el.value || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
    if (!text || text.length > 80) continue;
    if (!el.dataset.apAct) el.dataset.apAct = 'a' + (window.__apA = (window.__apA || 0) + 1);
    out.push({ id: el.dataset.apAct, text, link: el.tagName === 'A' && el.getAttribute('role') !== 'button',
      dialog: !!el.closest('dialog[open], [role="dialog"], [aria-modal="true"]') });
  }
  return out;
}
"""

_APPLY = re.compile(r"^(easy apply|apply( now| for| to| on)?\b|start (your |my )?application|i'?m interested|autofill\b|"
                    r"(use|apply|fill)( it)?( in)? (with |from )?(my |your |a )?resume\b)", re.I)
_FROM_RESUME = re.compile(r"autofill|resume", re.I)
_NOT_APPLY = re.compile(r"similar|filter|apply to (other|more)", re.I)
_NEXT = re.compile(r"^(next|continue|proceed|review|save (and|&) (continue|next)|next step)\b", re.I)
_SUBMIT = re.compile(r"^(submit|send application|send my application|finish|complete application|apply)\b", re.I)
_BACK = re.compile(r"back|previous|cancel|draft|sign in|log ?in", re.I)
_CONFIRMED = re.compile(
    r"thank you for (applying|your application)|\bthank you\b|application (was |has been |is )?"
    r"(received|submitted|complete)|successfully (applied|submitted)|we('| ha)ve received your application|"
    r"your application (was|has been) sent",
    re.I,
)
_RECEIVED = re.compile(
    r"thank you for (applying|your application)|application (was |has been |is )?(received|submitted)|"
    r"successfully (applied|submitted)|we('| ha)ve received your application|your application (was|has been) sent",
    re.I,
)
REGISTER_LINK = re.compile(r"create (an |your |new )?account|\bregister\b|sign ?up|new (user|candidate|account)", re.I)
REGISTER_SUBMIT = re.compile(r"^(create|register|sign ?up|submit|continue|next)\b", re.I)
CODE_SUBMIT = re.compile(r"^(verify|validate|confirm|continue|next|submit|sign ?in|log ?in)\b(?!.*(application|apply))", re.I)
_CODE_LABEL = re.compile(
    r"(verification|security|confirmation|authentication|login|access) code|one[- ]time (pass)?code|one[- ]time password|"
    r"\botp\b|passcode|(enter|type) (the |your )?(\d+[- ](digit|character) )?code|\d[- ](digit|character) code|^code$",
    re.I,
)
_TERMS = re.compile(r"\b(terms|conditions|privacy (policy|notice|statement|terms)|data (privacy|protection|processing))\b", re.I)
_NOT_TERMS = re.compile(r"marketing|newsletter|promot|offers|job alert|talent (pool|community)|searchable|other (positions|roles|jobs)|"
                        r"true and accurate|certif|authori[sz]ed to work|years of age|18", re.I)
_CODE_KINDS = ("text", "tel", "number")
_MIN_SPLIT_BOXES = 4
_USERNAME = (
    'input[type="email"]:visible, input[autocomplete="username"]:visible, input[name*="user" i]:visible, '
    'input[name*="email" i]:visible, input[name*="login" i]:visible, input[id*="user" i]:visible, '
    'input[id*="email" i]:visible, input[id*="login" i]:visible, input[autocomplete="email"]:visible, '
    # Workday's email box is a plain text box named only by these.
    'input[data-automation-id*="email" i]:visible, input[aria-label*="email" i]:visible'
)
SIGN_IN_LINK = re.compile(r"^\s*(sign|log) ?in\b", re.I)
_SIGN_IN_PATH = re.compile(r"/(login|signin|sign-in|authwall|checkpoint|uas/|sso/)", re.I)
_SIGN_IN_DIALOG = ('[role="dialog"]:has-text("sign in"), [role="dialog"]:has-text("log in"), '
                   '.contextual-sign-in-modal:has-text("sign in"), .modal__overlay--visible:has-text("sign in")')
_CAPTCHA = (
    'iframe[src*="recaptcha"]:not([src*="size=invisible"]), iframe[src*="hcaptcha"], '
    'iframe[src*="challenges.cloudflare.com"], '
    # A submit button can carry this class for a check that only runs if it is clicked.
    # The invisible reCAPTCHA badge, and the logo box inside it, never asks the person anything.
    '[class*="captcha" i]:not(button, a, input, sdf-button, [role="button"], .grecaptcha-badge, .grecaptcha-badge *)'
)


def _frames(page: Page):
    return list(page.frames)


async def scan(page: Page) -> list[dict]:
    fields = []
    for index, frame in enumerate(_frames(page)):
        try:
            found = await frame.evaluate(SCAN_JS)
        except PlaywrightError:
            continue
        for field in found:
            field["frame"] = index
            fields.append(field)
    return fields


def _locator(page: Page, field: dict, element_id: str):
    frames = _frames(page)
    frame = frames[field["frame"]] if field["frame"] < len(frames) else page.main_frame
    return frame.locator(f'[data-ap-id="{element_id}"]')


def _open_options(page: Page, field: dict):
    frames = _frames(page)
    frame = frames[field["frame"]] if field["frame"] < len(frames) else page.main_frame
    return frame.locator('[role="listbox"]:not([data-automation-id="selectedItemList"]) [role="option"]:visible')


async def load_options(page: Page, field: dict) -> list[dict]:
    """Opens a custom dropdown to read its choices, then closes it without choosing."""
    await _locator(page, field, field["id"]).click()
    options = _open_options(page, field)
    try:
        await options.first.wait_for(timeout=3000)
    except PlaywrightError:
        await page.keyboard.press("Escape")
        return []
    labels = [" ".join(text.split()) for text in await options.all_inner_texts()]
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(200)
    return [{"label": label, "value": label} for label in labels if label and not re.match(r"(select|choose)\b", label, re.I)]


async def fill(page: Page, field: dict, value: str, option: dict | None = None) -> None:
    kind = field["kind"]
    if kind == "file":
        await _locator(page, field, field["id"]).set_input_files(value)
    elif kind == "checkbox":
        box = _locator(page, field, field["id"])
        try:
            await box.set_checked(value == "Yes", force=True)
        except PlaywrightError:
            # Portals redraw a page as it finishes loading, which can swallow the first click.
            await page.wait_for_timeout(1500)
            await box.set_checked(value == "Yes", force=True)
    elif kind == "select" and field.get("listbox"):
        await _locator(page, field, field["id"]).click()
        await _open_options(page, field).filter(has_text=re.compile(rf"^\s*{re.escape(option['label'])}\s*$")).first.click()
        await page.wait_for_timeout(300)
    elif kind == "select":
        await _locator(page, field, field["id"]).select_option(value=option["value"])
    elif kind == "radio":
        await _locator(page, field, option["id"]).check(force=True)
    elif kind == "checkbox_group":
        await _locator(page, field, option["id"]).set_checked(True, force=True)
    elif kind == "combobox":
        box = _locator(page, field, field["id"])
        await box.fill(value)
        await page.wait_for_timeout(600)
        choice = page.get_by_role("option", name=value).first
        if await choice.count():
            await choice.click()
        else:
            await box.press("Enter")
    else:
        await _locator(page, field, field["id"]).fill(value)


async def clear(page: Page, field: dict) -> None:
    if field["kind"] not in ("radio", "checkbox_group", "file", "select", "checkbox"):
        await _locator(page, field, field["id"]).fill("")


async def find_actions(page: Page) -> dict[str, dict | None]:
    """Pick the apply, next and submit controls. Next wins over submit when both show."""
    found = {"apply": None, "next": None, "submit": None}
    for index, frame in enumerate(_frames(page)):
        try:
            actions = await frame.evaluate(ACTIONS_JS)
        except PlaywrightError:
            continue
        # An open popup covers the page, so its buttons come first, and the site's own fill from a
        # resume wins over applying manually.
        for action in sorted(actions, key=lambda a: (not a["dialog"], not _FROM_RESUME.search(a["text"]))):
            action["frame"] = index
            text = action["text"]
            if _BACK.search(text):
                continue
            if not found["apply"] and _APPLY.search(text) and not _NOT_APPLY.search(text):
                found["apply"] = action
            if action["link"]:
                continue
            if not found["next"] and _NEXT.search(text):
                found["next"] = action
            if not found["submit"] and _SUBMIT.search(text):
                found["submit"] = action
    return found


async def click(page: Page, action: dict) -> None:
    frames = _frames(page)
    frame = frames[action["frame"]] if action["frame"] < len(frames) else page.main_frame
    await frame.locator(f'[data-ap-act="{action["id"]}"]').click()


async def page_text(page: Page) -> str:
    try:
        return await page.evaluate("() => document.body ? document.body.innerText : ''")
    except PlaywrightError:
        return ""


async def is_confirmation(page: Page) -> bool:
    return bool(_CONFIRMED.search(await page_text(page)))


async def application_received(page: Page) -> bool:
    """Unmistakable wording only, for use straight after submitting, even if other prompts share the page."""
    return bool(_RECEIVED.search(await page_text(page)))


async def blocker(page: Page) -> str | None:
    """Sign-in walls and CAPTCHAs are always handed to the person, never worked around."""
    for frame in _frames(page):
        try:
            if await frame.locator(_CAPTCHA).first.is_visible(timeout=200):
                return "captcha"
        except PlaywrightError:
            pass
        try:
            if await frame.locator('input[type="password"]').first.is_visible(timeout=200):
                return "sign_in"
        except PlaywrightError:
            pass
        try:
            # A sign-in popup covers the page, and its buttons underneath cannot be clicked.
            if await frame.locator(_SIGN_IN_DIALOG).first.is_visible(timeout=200):
                return "sign_in"
        except PlaywrightError:
            pass
    if _SIGN_IN_PATH.search(page.url.split("?")[0]):
        return "sign_in"
    return None


async def password_count(page: Page) -> int:
    total = 0
    for frame in _frames(page):
        try:
            total += await frame.locator('input[type="password"]:visible').count()
        except PlaywrightError:
            pass
    return total


async def find_button(page: Page, pattern: re.Pattern) -> dict | None:
    for index, frame in enumerate(_frames(page)):
        try:
            actions = await frame.evaluate(ACTIONS_JS)
        except PlaywrightError:
            continue
        for action in actions:
            if pattern.search(action["text"]):
                return {**action, "frame": index}
    return None


async def sign_in(page: Page, host: str, email: str, password: str) -> bool:
    """Type saved details, only into frames on the exact host. Handles email-then-password pages."""
    for frame in _frames(page):
        if (urlparse(frame.url).hostname or "").lower() != host:
            continue
        # A sign-in pop-up can sit over a sign-up form, as on Workday, so its boxes come first.
        scope = frame
        dialog = frame.locator('[role="dialog"]:visible, dialog[open]').filter(has=frame.locator('input[type="password"]'))
        if await dialog.count():
            scope = dialog.last
        secret_box = scope.locator('input[type="password"]:visible').first
        user_box = scope.locator(_USERNAME).first
        if await user_box.count():
            await user_box.fill(email)
            if not await secret_box.count():
                await user_box.press("Enter")
                await page.wait_for_timeout(1500)
        if await secret_box.count():
            await secret_box.fill(password)
            await secret_box.press("Enter")
            return True
    return False


def is_terms_box(field: dict) -> bool:
    """A box accepting a site's terms. Marketing and statements of fact stay with the person."""
    label = field["label"]
    return field["kind"] == "checkbox" and bool(_TERMS.search(label)) and not _NOT_TERMS.search(label)


def code_boxes(fields: list[dict]) -> list[dict]:
    """The box for an emailed code: one labeled box, or a row of one-character boxes."""
    for field in fields:
        if field["kind"] in _CODE_KINDS and (field.get("one_time_code") or _CODE_LABEL.search(field["label"])):
            return [field]
    run: list[dict] = []
    for field in fields:
        if field["kind"] in _CODE_KINDS and field.get("max_length") == 1 and (not run or run[-1]["frame"] == field["frame"]):
            run.append(field)
        elif len(run) >= _MIN_SPLIT_BOXES:
            break
        else:
            run = []
    return run if len(run) >= _MIN_SPLIT_BOXES else []


def field_url(page: Page, field: dict) -> str:
    frames = _frames(page)
    return frames[field["frame"]].url if field["frame"] < len(frames) else page.url


async def enter_code(page: Page, boxes: list[dict], code: str) -> None:
    if len(boxes) == 1:
        await fill(page, boxes[0], code)
        return
    for box, character in zip(boxes, code):
        await fill(page, box, character)


async def send_code(page: Page, boxes: list[dict]) -> None:
    button = await find_button(page, CODE_SUBMIT)
    if button:
        await click(page, button)
    else:
        await _locator(page, boxes[-1], boxes[-1]["id"]).press("Enter")


async def validation_errors(page: Page) -> list[str]:
    errors = []
    for frame in _frames(page):
        try:
            texts = await frame.locator('[role="alert"], .error, .error-message, .invalid-feedback').all_inner_texts()
        except PlaywrightError:
            continue
        errors += [t.strip() for t in texts if t.strip()]
    return errors


def signature(url: str, fields: list[dict]) -> str:
    return url.split("#")[0] + "|" + "|".join(sorted(f["label"] for f in fields))


def declaration_text(text: str) -> str:
    """The sentences that submitting would agree to, so the person can read them first."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    hits = [l for l in lines if re.search(r"declar|certif|true and accurate|i agree|acknowledge", l, re.I)]
    return " ".join(hits)[:1500]

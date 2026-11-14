# UI guide: keep every screen looking like one product

The UI is plain JS and one stylesheet (`clipcrow/service/static/`). It was audited and redesigned page by page; new work must look like it
was always there. Read this before adding or changing anything a person sees.

## Rules
1. **Tokens only.** Colours, radii, spacing and shadows come from the variables at the top of `style.css` (`--surface`, `--line-strong`,
   `--accent`, `--r-sm`, ...). No literal colours in `app.js` or below the token blocks in `style.css`. A new look is a new token, defined for BOTH themes.
2. **Reuse the parts.** Page = `.card`s; field = `label.field` + control + `.hint`; yes/no option = `label.check`; action = `.btn` (`.primary` once per
   screen); status = `.chip`; empty and error states have their own classes. Look for an existing class before writing a new one.
3. **Every control is a field.** A text-like input (text, number, password, email, date, datetime-local, time, select, textarea) gets the field
   look automatically: `style.css` styles inputs by EXCLUSION, so a new input type is never left as the browser default. A control the browser
   draws itself (a file picker, a date picker's segments and icon, a range, a colour well, a scrollbar) must be themed in `style.css` too.
   The popup a browser opens (the calendar) is owned by the browser: it follows `color-scheme`, which we set per theme; do not try to restyle it,
   and do not swap in a third-party widget (the CSP forbids third-party requests and there is no build step).
4. **Both themes, small screens.** Dark is the default, light follows the system. Check a new screen in both and at 360 px wide (no horizontal scroll).
   Text keeps at least 4.5:1 against the surface under it. Keep a visible focus ring (`:focus-visible` is global; do not remove it).
5. **No job data in markup.** Build nodes with `el()` and set text with `textContent`; no `innerHTML`, no inline handlers, no inline styles (a test enforces it).
6. **Plain words.** Say what the thing does for the person, not how it works. Errors say what to do next. Marathi and Hindi copy stays Devanagari.
7. **Icons** are CSS masks (`.ico-*`), so there is no inline SVG markup and no `http://` in `app.js`.

## Adding a control: checklist
- Is there a class for it already? Use it. Otherwise add the CSS next to its relatives in `style.css`, with tokens, and a hover, focus and disabled state.
- Open it in dark and light (a Playwright screenshot is enough: `deviceScaleFactor: 2`) and compare it with the text field next to it: same height, border,
  radius, background, focus ring.
- New input type used in `app.js`? Add it to `STYLED_INPUT_TYPES` in `tests/test_ui_consistency.py` after you have checked it looks right. That test is
  there so a new kind of control cannot ship unstyled.

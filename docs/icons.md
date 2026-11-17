# Icons

`clipcrow/service/static/icons.css` is generated; do not edit it by hand. Each icon is a CSS mask
(`.i-<name> { --m: url("data:image/svg+xml,...") }`) used with the `.i` class from `style.css`, so it
takes `currentColor`. Everything is inlined as a `data:` URI because the content-security policy allows
no external requests. `.i-upload` and `.i-film` stay in `style.css` and are not emitted here.

## Regenerate

```bash
python scripts/make_icons.py     # needs network once (raw.githubusercontent.com); never run at runtime or in tests
```

Bump `LUCIDE_TAG` / `SIMPLE_TAG` in the script to update, check the render of every icon, then commit
`icons.css`. Add an icon by adding its name to `LUCIDE` or `PLATFORMS` in the script.

## Sources

| Set | Source | Pinned | Licence | Classes |
|---|---|---|---|---|
| UI line icons | [Lucide](https://github.com/lucide-icons/lucide) | tag `0.577.0` | ISC | `.i-<name>` (stroke 1.8, round caps) |
| Platform logos | [Simple Icons](https://github.com/simple-icons/simple-icons) | tag `13.0.0` | CC0-1.0 | `.i-plat-<slug>` (filled) |

Notes: Lucide renamed `circle-help` to `circle-question-mark`; the script fetches the new file and keeps the
class `.i-circle-help`. Simple Icons removed `linkedin` in 14.0.0 at the brand's request, so the set is pinned
to 13.0.0, the last release that has it.

## Trademarks

Platform logos are trademarks of their owners. They are used only to identify the platform (for example on
a "publish to" button); no endorsement or affiliation is implied. The drawings are CC0, but the marks
themselves remain protected.

The licence texts are in `NOTICE`.

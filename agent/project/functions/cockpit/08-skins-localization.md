# Skins & Localization Function

## Description

Two display parameters that are pure data, not code: the panel's visual theme comes from CSS files in
`cockpit/skins/`, and its strings from JSON catalogues in `cockpit/i18n/`. Adding a new skin or a
new language requires editing neither the server nor the HTML — drop a file in the right folder and it
shows up automatically.

## Key Features

- **Skins auto-listing** (`skins()`): a `*.css` file in `cockpit/skins/` appears in the
  picker; the header comment supplies the display name, description and the core version, e.g.
  `/* name: … for: … about: … */`. A skin built for an older core sets `behind` when its `for:`
  minor version differs from `kit_version()`.
- **Skin serving** (`skin_css()`): only a file inside `cockpit/skins/` (`basename` + `.css`) is
  ever served, to keep arbitrary reads impossible.
- **Theming model** (documented in `cockpit/skins/README.md`): a skin sets a `:root` block of
  CSS custom properties (`--bg`, `--surface-1/2`, `--border`, `--text`, `--primary`,
  `--accent`, `--gold`, `--tier-*`, `--danger`, `--corruption`, `--glow`, `--radius`,
  `--mono`, `--sans`, `--aura-1/2`, `--console-*`) and an optional
  `:root[data-theme="light"]`/`dark` variant. Skins load *after* the base stylesheet, so they
  may override tokens; they should not move layout. Shipped skins: `contrast`, `paper`, `vault`,
  `zine`; the default is «Зин».
- **Language auto-listing** (`languages()`, `i18n_catalogue()`): a `*.json` in `cockpit/i18n/`
  appears as a language (from its `_name`); missing keys fall back to Russian rather than to empty/keys;
  a broken catalogue rolls back to Russian with a warning.
- **Default embedded Russian**: the server injects the Russian catalogue into the HTML
  (`replace('"__AURORA_I18N__"', json.dumps(i18n_catalogue(DEFAULT_LANG)...))`) so the
  panel never depends on a network round-trip before first paint.
- **UI wiring** (`loadI18n`, `applyI18n`, `t()`): reads the saved language from
  `localStorage` (`aurora-lang`), fetches others via `/api/i18n`, applies `data-i18n` /
  `data-i18n-ph` annotations; `DEFAULT_LANG = "ru"`.

## Related Documentation

### Technical Details
- [Cockpit Architecture Design](../../design/01-cockpit-architecture.md) - skins/i18n as data-driven resources
### Source Files
- `cockpit/aurora_cockpit.py` - `skins`, `skin_css`, `SKINS_DIR`, `languages`, `i18n_catalogue`, `DEFAULT_LANG`, `I18N_DIR`
- `cockpit/skins/README.md` - the token reference and the "make your own" guide
- `cockpit/skins/*.css` - `contrast`, `paper`, `vault`, `zine`
- `cockpit/i18n/ru.json` - the default (Russian) string catalogue
- `cockpit/ui/index.html` - `loadI18n`, `applyI18n`, `t()`, skin selector `#skinSel`, theme toggle `#themeBtn`

### Related Functions
- [Server & Launch](./01-server-and-launch.md) - `/api/skins`, `/api/skin`, `/api/i18n` routes and token injection

## Implementation Notes

Both mechanisms share the same "file in a folder = capability" pattern the project uses elsewhere
(themes, languages, quick-start scenarios): discovery reads from the filesystem, never a hardcoded list,
so adding one is additive. Skins are applied by setting `#skin` `<style>` id's text from `/api/skin`;
the analyst's full theme (dark/light) toggles independently via `data-theme`.

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, cockpit*
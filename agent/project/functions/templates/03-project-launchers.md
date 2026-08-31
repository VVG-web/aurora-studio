# Project Launchers
## Description
Two double-click launchers let a user run daily Aurora project operations without a terminal. `start-aurora.bat` targets Windows (`@echo off`, `chcp 65001` for UTF-8) and `start-aurora.command` targets macOS/Linux (`#!/bin/bash`). Both `cd` into the project folder, locate a Python 3 interpreter and the Aurora kit, then present the same six-item interactive menu: doctor, stats, setup, cockpit, cockpit restart, and the command reference.

## Key Features
- **Python discovery** — Windows tries `py -3` then `python`; the shell script tries `python3` then `python`; both print an install hint if missing.
- **Kit discovery** — searches `{{KIT_PATH}}`, `$HOME/aurora-studio` / `%USERPROFILE%\aurora-studio`, and a sibling `aurora-studio` folder looking for `aurora.py` (`KIT_HINT` / `{{KIT_PATH}}` = `{{KIT_PATH}}` placeholder).
- **Interactive menu** with choices:
  1) `aurora_doctor.py` — project readiness;  2) `aurora_stats.py` — knowledge-base health;
  3) `aurora_setup.py` — configure Confluence, Jira, privacy;
  4) `aurora.py cockpit --add-root <root>` — open the control panel in the browser;
  5) `aurora.py cockpit --restart --add-root <root>` — restart the panel after a kit update;
  6) `kit_commands.py` — command reference;  0) exit.
- Graceful fallback messages when the kit is not found near the project.

## Related Documentation
### Technical Details
- [Design doc](../../design/04-templates-layout-generation.md) - template organisation and placeholder substitution
### Source Files
- templates/launchers/start-aurora.bat - Windows batch launcher
- templates/launchers/start-aurora.command - macOS/Linux shell launcher
### Related Functions
- [Project Configuration & Secrets](./01-project-configuration-secrets.md) - setup step reconfigures the values this menu drives

## Implementation Notes
The `{{KIT_PATH}}` placeholder is the launcher-specific substitution (distinct from the config/agent placeholders) and must be rewritten at generation time. Menu labels and inline help strings are in Russian.

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, templates*
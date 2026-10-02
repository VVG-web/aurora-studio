# Windows and macOS

[Wiki home](README.md) · related: [Troubleshooting](Troubleshooting.md) · [The card](The-Card.md#rules-worth-remembering)

The kit is built on Linux and tested on every push on **Linux (Python 3.12 and the oldest supported 3.9) and Windows
(`windows-latest`)**. A project is opened by people on Windows, macOS and Linux at once, so the rule for names is the
strictest of the three.

## File names must pass everywhere

| Rule | Why |
|---|---|
| no `< > : " / \ \| ? *` | Windows refuses them |
| not `CON`, `NUL`, `COM1`, … | reserved on Windows |
| at most **255 bytes** per name (Cyrillic is two bytes a letter) | macOS and Linux limits |
| a path from the project root up to **200 characters** | Windows' 260 limit, with a margin |
| names in one folder must differ by more than case | macOS and Windows treat `A` and `a` as one file |
| letters in NFC | macOS creates NFD names; the same word then looks different to Linux |
| no trailing dot or space | Windows strips them |

The check is `kit:doctor`; the fix is `kb:names` (preview, then `--apply`). Mirror paths are shortened by the sync itself
when a deep Confluence branch would not fit.

## Windows

- **Launch:** double-click `start-aurora.bat`. It looks for Python, offers to install what is missing and opens the panel.
- **Python:** the launchers try `python` and `py` before trusting a stub; Python 3.9+ and git are required.
- **Open files cannot be replaced.** Windows refuses to replace a file another process is reading. The engine's atomic writes
  (manifest, vector index, settings) wait and retry instead of failing.
- **Another process's pid.** The writing-run lock asks the system whether the holder is alive through the Windows API,
  never by sending it a signal (a "signal 0" on Windows would stop the process).
- **Encoding.** The console and pipes are in cp1251/cp1252; the engine switches its own output and its children's to UTF-8,
  so Russian text and `—` are not lost.
- **Paths** in the engine's own reports and in the panel use `/` everywhere, whatever the system.
- **Symlinks** need a privilege. The skill installer copies skills into `~/.claude/skills` and, if it cannot create a link for
  another harness, says so and carries on.
- **Line endings.** A git checkout with `autocrlf` turns kit files into CRLF; the engine reads them as text, so this is harmless.
- **Different drives.** A project on `D:` while the shell is on `C:` works: the engine does not assume one drive.

## macOS

- **`start-aurora.command` is blocked** by Gatekeeper the first time: right-click → *Open*.
- **System Python is 3.9**, which is the lowest supported version. The launcher recognises the "Xcode command line tools"
  stub as "not Python" and says what to install.
- **Case-insensitive file system.** Renaming `raw` → `Raw` takes two steps: `mv raw tmp && mv tmp Raw`.
- **NFD names.** macOS stores accented and Cyrillic names decomposed; `kit:doctor` finds them and `kb:names` normalises.

## What is guaranteed by tests

The same suite runs on Windows in CI, in three parallel parts. It covers locks, atomic writes, paths with `\`, UTF-8 pipes,
files with Cyrillic names under `.gitignore`, a terminal that is really `NUL`, and projects on another drive. A Windows
problem that is not in this list is a bug — please open an issue.

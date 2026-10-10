# Windows and macOS

[Wiki home](README.md) · related: [Troubleshooting](Troubleshooting.md) · [The card](The-Card.md#rules-worth-remembering)

The kit is built on Linux and tested on every push on **Linux (Python 3.11–3.14), Windows
(`windows-latest`; Python 3.12 blocks a release, 3.14 does not yet) and macOS (`macos-latest`; does not block a release yet)**. A project is opened by people on Windows, macOS and Linux at once, so the rule for names is the
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
- **Python:** the launchers try `python` and `py` before trusting a stub; Python 3.11+ and git are required (3.10 works with a warning).
- **Installing what is missing:** `start-aurora.bat` offers `winget install -e --id Python.Python.3.14` and `winget install -e --id Git.Git` (it asks first, `[y/N]`). `winget` must be available; without it the file prints the download pages (python.org, git-scm.com) and stops. After a `winget` install close the window and run the file again: the new `PATH` is visible only to new windows.
- **The Microsoft Store shortcut:** a `python.exe` from `WindowsApps` opens the Store instead of running Python. The file checks the interpreter by running it, not by finding it on `PATH`, so such a shortcut is not taken for Python.
- **Open files cannot be replaced.** Windows refuses to replace a file another process is reading. The engine's atomic writes
  (manifest, vector index, settings) wait and retry instead of failing.
- **Another process's pid.** The writing-run lock asks the system whether the holder is alive through the Windows API,
  never by sending it a signal (a "signal 0" on Windows would stop the process).
- **Encoding.** The console and pipes are in cp1251/cp1252; the engine switches its own output and its children's to UTF-8,
  so Russian text and `—` are not lost.
- **Paths** in the engine's own reports and in the panel use `/` everywhere, whatever the system.
- **Symlinks** need a privilege. The skill installer copies skills into `~/.claude/skills` and, if it cannot create a link for
  another harness, says so and carries on.
- **There is no `python3`.** Python from python.org and from `winget` installs `python` and the `py` launcher, not `python3`; a `python3` from `WindowsApps` is a Store stub. Run scripts as `py -3 script.py` or `python script.py`. The git hooks and the project's `start-aurora.bat` pick the interpreter by running it (`python3`, `python`, `py -3`); the engine itself then checks the version and stops with a message if it is too old. The git hooks, the kit's `start-aurora.bat` and `start-aurora.command` and the project's `start-aurora.bat` look for Python 3.10 or newer (the project's `start-aurora.command` does not check the version: the engine itself stops with a message); if the linter exits with any code other than 0 and 1 (for example it stops on the version, crashes with a traceback, or the project's `kb_lint.py` is older than the hook and does not know an argument), the pre-commit hook prints the linter's own output and the line "the linter exited with code N - see the output above"; the pass over the commit then falls back to the full pass over the knowledge base (the ratchet and the block mode work as before), and if that one fails too, the check is skipped and the commit is not blocked (if the output names the Python version, install Python 3.11+).
- **Git hooks need Git for Windows.** The hooks are `sh` scripts run by the shell that ships with Git for Windows; without it they do not run. `start-aurora.bat` checks for git.
- **No execute bit and no `0600`.** NTFS has no execute bit, so launch scripts through `py -3`. The `0600` mode on files with keys and tokens protects nothing on Windows (access follows the folder's permissions): keep the project in your user profile, not in a shared folder.
- **Line endings.** A git checkout with `autocrlf` turns kit files into CRLF; the engine reads them as text, so this is harmless.
- **Different drives.** A project on `D:` while the shell is on `C:` works: the engine does not assume one drive.

## macOS

- **`start-aurora.command` is blocked** by Gatekeeper the first time: right-click → *Open*.
- **System Python is 3.9** (from the Xcode command line tools) and is **not supported**: it cannot be updated, and the
  engine stops at once with a message saying what to install. Install Python 3.11+ without touching the system one:
  `brew install python@3.14`, the installer from python.org (then open `Install Certificates.command`), or `uv python install 3.14`
  if you have no administrator rights. The launcher recognises the "Xcode command line tools" stub as "not Python" and says what to install.
- **The "update the kit" button and an old Python.** Before it writes anything the button reads the minimum Python of the *new* version; if the panel runs on an older one it refuses with the install hint and changes nothing. Restarting the panel first checks that the new version starts on this interpreter: if it does not, the old panel stays alive and the message shows the tail of the error instead of a promise that the panel will come back. If the "Health" doctor itself fails to start, the page shows the reason instead of an empty list of blockers.
- **Case-insensitive file system.** Renaming `raw` → `Raw` takes two steps: `mv raw tmp && mv tmp Raw`.
- **NFD names.** macOS stores accented and Cyrillic names decomposed; `kit:doctor` finds them and `kb:names` normalises.

## What is guaranteed by tests

The same suite runs on Windows in CI, in three parallel parts. It covers locks, atomic writes, paths with `\`, UTF-8 pipes,
files with Cyrillic names under `.gitignore`, a terminal that is really `NUL`, and projects on another drive. A Windows
problem that is not in this list is a bug — please open an issue.

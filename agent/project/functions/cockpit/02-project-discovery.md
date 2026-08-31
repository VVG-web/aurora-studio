# Project Discovery Function

## Description

Finds every Aurora project on the machine and shows them all at once on the «Мостик» (bridge)
overview screen. The panel watches a user-editable list of search roots, walks them for
`aurora.config.yaml`, builds a visual "tower" tile per project (engine version, verified-knowledge
share, doctor blockers, linter errors, branch, uncommitted changes), and colors the project's aura by
the severity of its problems.

## Key Features

- **Search roots** are stored per user in `~/.aurora/cockpit-roots.txt` (`ROOTS_FILE`) and
  loaded via `load_roots()`: from `--roots` (one-off, not saved), then the file, then the
  folder beside the kit. `save_roots()` and `allowed_bases()` define where deployment is legal.
- **Discovery** via `find_projects(roots, depth=3)`: `os.walk` over roots, pruning tool
  folders (`node_modules`, `__pycache__`, `Sources`, `Raw`, `AuroraKnowledgeDB`, `Artifacts`,
  `Deliverables`, `Workspaces`, `Templates`, `Prompts`) and dot-dirs, up to `depth` levels;
  a path counts when it contains `aurora.config.yaml`.
- **Project card** (`project_card()`) reads the config (`name`, `slug`, `space`, `project_key`,
  `scrub`), the engine version from the project's `AuroraKnowledgeDB/meta` version file, and the
  `.env.aurora.local` to report which
  module tokens are filled (never their values) plus the git branch and dirty-file count.
- **The «Мостик» screen** (`view-overview` in `cockpit/ui/index.html`) renders one clickable
  tile per project with an aura color and a progress ring fed by `/api/state` → `projects`.

## Related Documentation

### Technical Details
- [Cockpit Architecture Design](../../design/01-cockpit-architecture.md) - `_known()` validation and roots model
### Source Files
- `cockpit/aurora_cockpit.py` - `load_roots`, `save_roots`, `allowed_bases`, `find_projects`, `project_card`, `git_branch`, `git_dirty_count`, `norm`, `writable_target`
- `cockpit/ui/index.html` - `view-overview` markup, token `.tower`, `.ring`, aura coloring
- `cockpit/skins/README.md` - how skins/terns affect the overview look

### Related Functions
- [Server & Launch](./01-server-and-launch.md) - `--roots`, `--add-root` flags feeding discovery
- [Health Dashboard](./03-health-dashboard.md) - per-project health metrics reachable from a tile

## Implementation Notes

`find_projects` is also the source of truth for access control: the POST handlers accept a project
only when `self._known(project)` finds its path among `find_projects(self.server.roots)`, so an
arbitrary filesystem path is never accepted. The aura severity color is derived from per-project
`doctor` blockers and linter errors surfaced in the health payload.

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, cockpit*
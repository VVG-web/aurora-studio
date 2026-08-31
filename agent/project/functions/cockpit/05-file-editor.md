# File Editor Function

## Description

The «Файлы» (files) section is a full project file browser and markdown editor. It shows the
Aurora project tree as-is, marks read-only and "outside the editor" files, and lets the analyst open,
create, rename, delete, save, publish, lint and reveal files — keeping knowledge-base invariants
enforced as the file is edited.

## Key Features

- **Tree** (`file_tree()` in server; `treeOf`/`drawTree` in UI): walks the project, skipping
  `__pycache__`/`node_modules`/`.DS_Store` and dot entries, marks `text` (in `TEXT_EXT`,
  under `MAX_EDIT`) vs. binary, shows recent files first, and offers filters (`изменённые`, `база`,
  `артефакты`, `черновики`).
- **Read-only rules** (`why_readonly()` / `READONLY`): `Deliverables/released`,
  `Raw/contract`, `Raw/meetings`, `Raw/laws`, `Raw/customer`, `Sources` are immutable with an
  explanation; `AuroraKnowledgeDB/` cards derived from sources are read-only (except
  `KB_WRITABLE`: Decisions, Questions, meta) and `kind: document` bodies can't be rewritten.
- **Create/rename/delete** (`file_create`, `file_rename`, `file_delete`, `why_no_create`,
  `MAKEABLE`): only legal folders accept new files; knowledge-base deletion is refused (`kb:supersede`);
  renames warn that `[[…]]` references break and point to `kb:repair`.
- **Read/edit/save** (`file_read`, `file_write`, `clean_preview`, `lint_one`, `recent`): atomic
  save via temp file + `os.replace`, optimistic-concurrency with a digest (`expect`), post-save
  lint via the project's `kb_lint.py --only` script, publication staleness via `file_changed_since_publish`,
  and a recent-files list.
- **Editor UI** uses the vendored Vditor editor with «Разметка и вид»/«Как в Word» modes, a
  frontmatter panel, preview cleaning through `clean_preview`, and a reveal via system file manager.

## Related Documentation

### Technical Details
- [Cockpit Architecture Design](../../design/01-cockpit-architecture.md) - `inside()`/`_known()` containment and single-file UI
### Source Files
- `cockpit/aurora_cockpit.py` - `file_tree`, `file_read`, `file_write`, `file_create`, `file_rename`, `file_delete`, `file_inside` helpers, `clean_preview`, `lint_one`, `why_readonly`, `why_no_create`, `recent`, `file_changed_since_publish`, `read_text`, `strip_frontmatter`, `reveal`
- `cockpit/ui/index.html` - `view-files`, Vditor editor, `renderFiles`, `drawTree`, `newFile`, `renameFile`, `deleteFile`
- `cockpit/vendor/vditor/` - vendored editor library served at `/vendor/vditor`

### Related Functions
- [Git & Kit Maintenance](./06-git-kit-maintenance.md) - commit/push from the files pane and git status pills
- [Health Dashboard](./03-health-dashboard.md) - lint numbers cross-referenced from the editor

## Implementation Notes

All file paths pass through `inside(root, path)`, which resolves symbolic links **before** comparison so
`Artifacts/ac/../../../../etc/passwd` and out-of-root symlinks are rejected. Writes are gated by
`why_readonly(rel, text)` and use a temp `*.aurora-tmp` + `os.replace` so an interrupted write
never leaves a truncated document. `recent` files live inside the project base's meta folder (`AuroraKnowledgeDB/meta`) so the list
travels with the base in git.

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, cockpit*
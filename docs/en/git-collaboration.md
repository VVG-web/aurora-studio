# Git without conflicts: local state, per-user shards, shared manifest

An Aurora project is opened by several people and several machines. One updates the
knowledge base, another only reads it, a third parses their own sources. If all of that
lives in one file, Git conflicts on every parallel edit. Files are therefore split into
three classes.

## LOCAL — machine state, outside Git

Panel recent files, Obsidian layout, the local retrieval checkpoint, the derived graph,
the engine version stamp and launcher files. They are not team knowledge and must not be
synchronised.

- The canonical `.gitignore` block adds them on install and on `kit:update`.
- Already tracked files are removed from the index with `git rm --cached` but **stay on
  disk**. A repeated run changes nothing.
- The list is owned by `scripts/aurora_update.py` (`LOCAL_STATE_RELS`), not by a human.

## SHARD — shared data, split per participant

Search-quality history, the knowledge-base update date, agent run journals and empty-run
notes. Appending to one shared file guarantees a conflict.

- `AuroraKnowledgeDB/meta/kb_updated.d/<actor>.json`
- `AuroraKnowledgeDB/meta/search-quality.d/<actor>.json`
- `AuroraKnowledgeDB/meta/agent-runs/<stamp>_<actor>_<task>.md`
- `AuroraKnowledgeDB/meta/agent-runs/_пустые/<actor>.md`

`scripts/aurora_actor.py` derives the participant identity: `AURORA_USER`, then git
`user.name`/`user.email`, then the OS user. The shard name is a safe slug plus a short
SHA-256; the email never appears in the name. Writers touch only their own shard; readers
merge the legacy file and all valid shards, deduplicate and sort deterministically. A
corrupt neighbour shard does not block the rest.

## SHARED — meaningful knowledge

Cards, decisions, requirements and `manifest.json` itself stay shared. They cannot simply
be ignored: that would lose work. `manifest.json` gets a domain merge.

## manifest.json: merge by source keys

`AuroraKnowledgeDB/meta/manifest.json` records parsed sources. Two participants almost
always change different `sources` keys, so a line-based merge reports a false conflict.

- `.gitattributes`: `AuroraKnowledgeDB/meta/manifest.json merge=aurora-manifest`.
- `scripts/merge_manifest.py` performs a three-way merge:
  - `sources` by key; if one side did not change an entry, the other side wins; if both
    changed it, the later `processed` wins; a deletion is kept when the other side is
    unchanged;
  - `cards` are unioned by canonical JSON;
  - `updated` and `version` take the maximum.
- `install_aurora.py` and `aurora_update.py` configure the git driver
  `merge.aurora-manifest.driver` in the project. Without git the step is skipped silently.

The driver never deletes data: on invalid JSON it returns code 1 and Git leaves the
conflict to a human.

## Rollback and mixed versions

- Rolling back the code leaves shards on disk and in Git. An older engine reads the legacy
  file but not the new shards; a full rollback needs an explicit consolidation.
- The `manifest.json` merge also works for older projects: the rule and driver are added
  by `kit:update`, and repeated runs are idempotent.

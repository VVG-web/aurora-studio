# Sources and mirrors

[Wiki home](README.md) · related: [The cycle](The-Cycle.md) · [Troubleshooting](Troubleshooting.md) · full text:
[source modules](../docs/en/connectors.md)

Aurora never reads Confluence or Jira while it builds cards. It first makes a **mirror** — a deterministic copy in
`Sources/` — and builds from that. Your own evidence lives in `Raw/`.

```mermaid
flowchart LR
  C["Confluence"] -->|"sync:confluence"| MC["Sources/Confluence"]
  J["Jira"] -->|"sync:jira"| MJ["Sources/JIRA"]
  W["Websites"] -->|"sync:web"| MW["Sources/Web"]
  R["Raw/<br>contract · laws · meetings ·<br>customer · project · corrections"] --> B
  MC & MJ & MW --> B["agent:build<br>parsing into cards"]
  R -->|"docx · pdf · xlsx · pptx<br>kb:ingest-office"| R
```

## Mirrors: deterministic, incremental, never edited by hand

- **The same source gives the same file, byte for byte.** Otherwise `git diff` stops being evidence and `sync:audit`
  cannot check the state. A repeated sync makes no noise in git.
- **Incremental.** A Confluence page with the same version, converter and path, and the file in place, is not
  downloaded again; once a week and with `--force` the mirror is passed in full.
- **Written only by sync.** Do not edit `Sources/` — the next sync would overwrite you; put your own words into
  `Raw/corrections/` (`kb:correct`) instead.
- **A failed sync keeps what it did not reach.** If the server timed out halfway, the pages the walk did not visit stay
  in the state, and `--prune` refuses to delete when the export had errors or the "extra" share is large.

## The bundled modules

| Module | Kind | Role | Mirror | Command |
|---|---|---|---|---|
| `confluence-dc` | wiki: folders repeat the page tree, a page with children → folder + `index.md` | documents | `Sources/Confluence/` | `sync:confluence` |
| `jira-dc` | board: a file per issue, the key is the name | tasks | `Sources/JIRA/` | `sync:jira` |
| `web` | board: a file per saved page | documents (trust per link) | `Sources/Web/` | `sync:web` |

More products (Notion, SharePoint, Confluence Cloud, YouTrack, GitLab Issues) are added as a folder in `connectors/`
with a `connector.json`; see [source modules](../docs/en/connectors.md).

## Settings and secrets

| What | Where |
|---|---|
| base URLs, spaces, `sync_roots` (page ids to walk from), project key, JQL | `aurora.config.yaml` (committed) |
| `CONFLUENCE_PERSONAL_TOKEN`, `JIRA_PERSONAL_TOKEN` (or user + password) | `.env.aurora.local` (git-ignored) |

Sync scripts talk to Confluence and Jira directly over REST. The Atlassian MCP in your editor does not help them: it
does not hand out its credentials. Check a token: `python3 .aurora/scripts/jira_export.py --limit 1`.

**A token goes only to its own server.** The address in `aurora.config.yaml` is shared by the team, and swapping it in
a pull request would carry the token away. So the REST client sends the key only to the server in `base_url`: an
absolute address that comes from the server (`_links.next`, an attachment) with another host is refused, and a redirect
to another host goes without `Authorization`. The key is never sent over plain `http://` (your own computer excepted).
The engine also remembers the server after the first successful reply (`.aurora/state/trusted-hosts.json`, not in git):
a different `base_url` afterwards is refused, with one line saying so, until you accept it. Accepting is one line in
`.env.aurora.local`: `AURORA_CONFLUENCE_SERVER=https://wiki.example.com` or `AURORA_JIRA_SERVER=https://jira.example.com`
(the same lines pin the server in advance — and for a project that has never synced, nothing is remembered yet, so set
them there). `AURORA_ALLOW_HTTP=1` allows plain `http://` for a server on a closed network. Model gateway keys and the Git password are protected differently: a redirect never carries
them to another host, a saved key is not carried over to a changed address, and the credential helper answers only the
repository's server; plain `http://` stays allowed there (a local Gitea or gateway on a closed network). The website crawl
token (`WEB_PERSONAL_TOKEN`) goes only to the host of the start page it is used with; a page added to the shared list
would get it, so list the hosts in `AURORA_WEB_TOKEN_HOSTS=wiki.example.com,…` and no other host receives it.

## Checking a mirror

| Command | Finds |
|---|---|
| `sync:audit` | `MISSING`, `ORPHAN`, `MOVED`, `COLLISION`, stale state, foreign files, headers with a repeated field or a forged `trusted` |
| `sync:diff` | drift — a source changed after the knowledge was built on it |
| `sync:jira-status` | issues closed while the requirement is not; work without requirements; links by mention |
| `kit:remap-sources`, `kb:repair --gone-sources` | a page moved: cards' `source:` follows it by `page_id` or document code |

## `Raw/` is evidence

Contracts, laws, meeting transcripts and customer material go into `Raw/{laws,contract,customer,project,meetings,
examples,corrections}`. By agreement it is **immutable** and counts as trusted without proof. Office formats are turned
into Markdown transcripts next to the original by `kb:ingest-office`; a scan is read by a vision model if one is
configured (there is deliberately no default model for scans — a guessed name gives a coherent fabrication).

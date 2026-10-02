# Getting started

[Wiki home](README.md) · next: [The cycle](The-Cycle.md)

## What you need

- **Python 3.9+** and **git**. The engine itself has no runtime dependencies (standard library only).
- Optional: `pandoc` (export to docx/pdf), `beautifulsoup4` + `markdownify` (Confluence sync), `markitdown`, `openpyxl`,
  `pypdf` (office documents). The panel's **Install** section shows what is missing and which commands it affects.
- Optional: an OpenAI-compatible LLM gateway for the built-in agent. Everything that does not need a model works without it.

## Install in three commands

```bash
git clone https://github.com/<org>/aurora-studio.git
cd aurora-studio
python3 aurora.py new /path/to/your-project     # deploy into a new or existing folder; answer the questions
python3 aurora.py cockpit                       # open the local control panel
```

No terminal? Double-click `start-aurora.command` (macOS) or `start-aurora.bat` (Windows): it checks Python and git,
offers to install what is missing and opens the panel. Without a terminal the setup questions are skipped and the
project is deployed with defaults — finish the settings later with `python3 aurora.py setup <project>` or in the panel.

## What you get

A project folder with a **fixed layout** (the same in every Aurora project): `Sources/` (mirrors, written only by sync),
`Raw/` (immutable evidence), `AuroraKnowledgeDB/` (the cards), `Artifacts/`, `Deliverables/`, `Workspaces/`, and
`.opencode/` with a copy of the engine. Details: [INSTALL](../docs/en/INSTALL.md#2-what-appears-in-the-project).

## The first fifteen minutes

1. **Check readiness.** In the panel, `kit:doctor` (or `python3 aurora.py doctor <project>`): it names what is missing
   — a git repository, the config, an `AGENTS.md`, a secret that must not be in git.
2. **Fill in the settings.** `aurora.config.yaml` holds URLs, spaces, project keys and JQL (it is committed);
   tokens go to `.env.aurora.local` (it is git-ignored). Copy `aurora.env.local.example` to start. See
   [INSTALL §3–4](../docs/en/INSTALL.md#3-settings-auroraconfigyaml).
3. **Open the project in the panel and press "Update the base".** It syncs the mirrors, builds cards in batches,
   writes theses, links the cards, computes trust and commits every lap to git. You may stop it at any time:
   what is done stays valid. → [The cycle](The-Cycle.md)
4. **Look at Health.** Share of `knowledge`, lint errors, what is left for a human (`ops:todo`).
5. **Ask something.** The **Ask** section answers from cards only and puts a link on every claim. → [Asking the base](Asking-the-Base.md)

## Where to go next

| If you are… | Read |
|---|---|
| an analyst working daily | [Daily work](Daily-Work.md), then [Practice](../docs/en/readme/04-practice.md) |
| setting up the team | [Team rules](../docs/en/readme/03-team-rules.md), [Looking after the base](../docs/en/readme/05-gardening.md) |
| connecting Confluence and Jira | [Sources and mirrors](Sources-and-Mirrors.md) |
| connecting an LLM | [The agent](The-Agent.md) |

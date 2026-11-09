# Parallel work with the LLM

Aurora runs every panel task in its own process. Two tabs and two projects therefore do not
share memory, a journal, or a task queue. Only the physical model-gateway slots and the
project files being written are shared.

## Tabs

Each tab keeps its own page state: selected project, open section, console, and form. A task
lives in the panel process, not in a tab, so closing a tab does not stop it. The console
above the list shows tasks from all projects, while `/api/jobs?project=...` returns tasks
for the selected project.

Starting the same command with the same arguments in the same project returns the running
task instead of creating a second one. This protects against a double click and a second tab
starting the same writing command.

## Projects

Tasks from different projects run at the same time. Inside a project, a writing run is
protected by `.aurora/state/agent.lock`: a second writer gets a refusal with the task name
and the holder pid. Read-only tasks do not remove another process's lock. A dead process
does not hold the lock, so a crashed run does not lock the project forever.

Artifacts, sessions, context cache, and journals stay in their own projects. One project
does not replace another's `Workspaces/`, `Artifacts/`, or `.aurora/cache/`.

## LLM slots

The gateway width declared in the model settings is the server limit, not a per-project
limit. Slots live in a machine-wide directory:

```text
~/.aurora/state/llm-slots/backend-<n>/slot-00.lock
```

`agent_core` takes a free slot through atomic `lock_take` and releases only its own file.
If a process crashes, the next `lock_take` sees the dead pid and removes the lock. Two
projects therefore cannot send more requests than the declared `width`, and a crashed
project does not leave a slot occupied.

The directory can be overridden with `AURORA_LLM_SLOTS_DIR`. In tests without that
variable, the local fair pool `_FairSlots` is used so tests do not depend on other running
projects.

When there are fewer slots than tasks, tasks wait. That is not an error: a gateway with one
slot physically cannot answer two requests at once. The panel shows that a task is waiting
for a slot, not that it failed.

## What remains sequential

- Two writing tasks in one project: the project lock.
- Requests to a gateway with `width: 1`: the physical gateway queue.
- Writing one card or one artifact: the engine lock and mechanical checks.

Different projects and different read-only tasks do not share these limits.

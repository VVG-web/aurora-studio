# Assignee Resolution Function
## Description
Determines who was the assignee of an issue at a given moment, used by both
`make_analyst_metrics.py` and `verify_weekly_by_person.py`. Previously each script carried its own
copy of `get_assignee_at` and they silently diverged; the logic now lives once in
`assignee_resolver.py` in the `AssigneeResolver` class.

The resolver consults several sources in order of decreasing reliability, because Jira does not write the
initial assignee or every responsible party into the changelog:

```python
def at(self, issue_key, at_ts):
    assignee = self._value_at(issue.get("assignee_history"), at_ts)
    if assignee:
        return assignee
    if not issue.get("assignee_history") and issue.get("assignee_now"):
        return issue["assignee_now"]
    return (self._value_at(issue.get("responsible_history"), at_ts)
            or issue.get("responsible_now")
            or self.analyst_by_subtasks(issue_key)
            or self.synced.get(issue_key)
            or issue.get("assignee_now"))
```

## Key Features
- **Changelog lookups.** `_value_at(history, at_ts)` walks the sorted field history and returns the
  last value set at or before the moment; an event earlier than the first changelog record is resolved
  to that record's `from` value (exact, not a guess). An empty history with a current assignee is
  taken to mean "assigned at creation and never changed".
- **«Ответственный» field.** Some stories record the responsible person in the custom «Ответственный»
  field rather than Assignee — its history is consulted next.
- **Analyst-by-subtasks.** `analyst_by_subtasks()` picks the analyst who authored the analytical
  subtasks of a story: subtask assignees are counted per parent and the analyst role from the roster
  is preferred (`is_analyst_subtask()` recognises `BA Sub-Task` type or analyst-like summaries, and
  excludes `[Back]/[Front]/[Design]` implementation and review subtasks).
- **Fresh sync mirror.** `load_synced_assignees()` reads the `assignee:` frontmatter of the
  `sync:jira` cards in `Sources/JIRA` (refreshed more often than the export) as a last resort,
  since a backdated assignee is still more accurate than "Не назначен".
- **One resolver per run.** The resolver is built once (in both consumer scripts) instead of per call,
  avoiding re-reading the whole `Sources/JIRA` folder hundreds of times.

## Related Documentation
### Technical Details
- [Analyst Report Pipeline Architecture](../../design/05-analyst-report-pipeline.md) - design overview
### Source Files
- reports/analyst/assignee_resolver.py - `AssigneeResolver`, source heuristics
- reports/analyst/make_analyst_metrics.py - consumer (assignee at transition time)
- reports/analyst/verify_weekly_by_person.py - consumer (per-person weekly buckets)
### Related Functions
- [Analyst Metric Computation](./04-analyst-metric-computation.md) - uses the resolver at event time
- [Data Fetching (Jira & Confluence)](./02-data-fetching.md) - produces `full_status.json` input

## Implementation Notes
The resolver is `static`-heavy and stdlib-only (`collections.Counter`/`defaultdict` for subtask
indexing). It drops nothing — unresolved issues fall through to `synced` then `assignee_now`, and the
dashboard later labels empty names as «Не назначен».

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, reports*
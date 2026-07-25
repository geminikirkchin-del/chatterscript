# Local Issue Tracker

This project uses a file-based issue tracker under `.scratch/`.

## Layout

```
.scratch/
└── <feature>/
    ├── spec.md          # Feature spec (PRD)
    └── issues/
        ├── 001-<title>.md
        ├── 002-<title>.md
        └── ...
```

## Issue file format

Each issue is a Markdown file with front matter:

```markdown
---
id: 001
title: Short title
status: ready-for-agent | in-progress | done
labels: ready-for-agent
blocks: []
blocked-by: []
---

# Title

## Description

...

## Acceptance Criteria

- [ ] ...
```

## Triage labels

- `ready-for-agent` — fully specified, can be picked up by an agent
- `needs-triage` — raw request, needs refinement
- `blocked` — has unresolved blockers
- `done` — completed

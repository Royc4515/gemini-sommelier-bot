# specs/ — Spec-Driven Development

This project is built spec-first. Specifications are the source of truth; the
agent implements from them rather than from ad-hoc prompts.

## The loop (one pass per feature)

```
Constitution  →  Specify  →  Plan  →  Tasks  →  Implement
 (this folder)    spec.md     plan.md  tasks.md  (code + tests + smoke)
                     ▲ human approval at every arrow ▲
```

1. **Specify** (`spec.md`) — WHAT & WHY: user stories, acceptance criteria,
   explicit non-goals. No implementation detail.
2. **Plan** (`plan.md`) — HOW: architecture, files touched, data shapes, risks,
   how each acceptance criterion is met — all checked against the constitution.
3. **Tasks** (`tasks.md`) — ordered, independently testable steps with
   checkboxes.
4. **Implement** — code task-by-task, keep the suite green, then a live smoke
   test for any external contract, then a PR.

Each artifact is approved before the next is written (constitution §9).

## Layout

```
specs/
  constitution.md           stable principles the agent is bound by
  templates/                blank artifacts to copy per feature
  NNN-feature-slug/         one folder per feature
    spec.md  plan.md  tasks.md
```

## Status

| # | Feature | Spec | Plan | Tasks | Done |
|---|---------|------|------|-------|------|
| 001 | Voice input + Tier-1 UX | drafted | — | — | — |

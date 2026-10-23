# Product direction: Career Thesis

**Status:** post-pivot product direction, clarified 2026-10-23. Career Radar and
the reviewed research stream are implemented as a local prototype. The personal
Career Map and Career Thesis remain planned.

## Product thesis

> **Career OS helps software engineers form and revisit a Career Thesis: a
> reasoned bet on where to invest the next 2–5 years of career capital, why that
> direction fits, and what would change the decision.**

The initial audience is senior software engineers choosing their next direction:
for example, deepening a specialty, moving into Staff-level ownership,
exploring AI product engineering, or pursuing engineering management.

The product hypothesis is that connecting changing external evidence to a
person's experience, goals, constraints, and preferences can make that choice
better informed and easier to revisit.

## What the pivot changed

The pivot moved the unit of decision from an individual vacancy to a career
trajectory. Public postings leave many facts about a specific job, team,
compensation package, and hiring outcome unknown. They can still supply bounded
observations about roles and skill mentions. Reviewed research adds evidence
about how developer work is changing, with its own scope and limitations.

The question is now: **Which direction deserves my next investment of time and
experience, why, and under what conditions should I reconsider?**

Collection is an evidence subsystem. Its value comes from supporting that
decision and its revision over time.

## Career Thesis as the product wedge

The intended wedge is a specific, recurring decision for an engineer facing
several plausible paths. A Career Thesis would turn the available evidence and
personal context into an explicit position the user can inspect and challenge.

| Layer | User question | Role in the product |
| --- | --- | --- |
| Career Radar | What is changing in the observed roles, skills, and developer work? | Evidence surface, useful on its own and an entry point to the personal layer |
| Career Map, planned | Which trajectories are plausible for me, and what are their trade-offs? | Comparison of alternatives using personal context and external evidence |
| Career Thesis, planned | Which direction will I invest in, why, and what would change my mind? | The reasoned decision and the basis for future review |

The recurring value would come from checking whether a previous thesis still
holds as the person and the evidence change. A new observation matters when it
strengthens, weakens, or calls for reconsidering an assumption behind that
decision.

## The intended output

A Career Thesis should make the following explicit:

- **Current bet and horizon:** the direction to explore or pursue and the
  career capital to build.
- **Personal rationale:** relevant experience, transferable strengths, goals,
  constraints, and preferences, with gaps in evidence visible.
- **External evidence:** the observations and reviewed claims supporting or
  challenging the bet, with sources, dates, and scope.
- **Alternatives and trade-offs:** other plausible paths and why the current
  direction is preferred.
- **Assumptions and risks:** what must hold for the reasoning to remain useful
  and what is still unknown.
- **Next experiment:** a bounded action that can test an important assumption
  or build missing evidence.
- **Revision conditions:** what new evidence or personal change would lead to
  a review, and how the reasoning changed from the previous thesis.

A fictional output might read:

> Explore a move from backend engineering toward AI product engineering while
> preserving systems depth. Test the fit through a production project with
> explicit evaluation and operational ownership. Compare this with a Staff
> backend path, and revisit the bet if hands-on experience conflicts with the
> preferred work or the external evidence no longer supports the assumptions.

This illustrates the output's shape; it is not a finding from the current data.
The user owns the decision. A thesis must distinguish observations, personal
preferences, and interpretation, and keep uncertainty visible.

## What exists today

The local prototype provides job collection, versioned evidence, role and skill
exploration, history-dependent comparisons, and a reviewed research stream.
Comparisons require sufficient saved history and coverage. Job-panel counts and
research populations retain separate denominators.

Personal synthesis, trajectory comparison, and thesis revision are future
capabilities. The [architecture overview](architecture.md) describes the
implemented foundation; the [ADR index](adr/README.md) records its evolution,
including the pivot and the research stream.

## How to validate the value

The product hypothesis earns support when a user can:

1. Explain a career bet and its alternatives using evidence relevant to their
   own goals and constraints.
2. Identify an assumption or trade-off that changes what they explore, learn,
   or pursue next.
3. Return after meaningful new evidence or a personal change and explain why
   their thesis should be retained, revised, or abandoned.

Each proposed source, metric, or feature should identify which part of that
decision becomes better supported. Collection volume alone does not validate
the product's value.

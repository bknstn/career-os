# Skill taxonomy ownership

`config/taxonomy/skills.v1.yaml` is the repository-owned, versioned inventory
of high-signal technical skills. A skill ID is lowercase snake_case, stable, and
never recycled: downstream evidence may retain that ID indefinitely. Labels and
aliases are display/evidence strings, while uniqueness is checked after Unicode
NFKC normalization, casefolding, and trimming.

The taxonomy uses semantic versions. Additive compatible changes increment the
minor version; corrections or alias/context changes increment the patch version;
an incompatible ID policy change requires a major version. Keep the declared
version and filename intentional rather than treating either as generated data.

Ambiguity is taxonomy-owned: a skill declares `ambiguous_aliases` as a subset of
its own aliases, and each declaration has exactly one top-level context rule.
Go, R, and C are the initial examples, but future declarations require no
Python policy change. Rules include one or more positive context regexes and
optional negative regexes; the corpus evaluates regex coverage with
case-insensitive matching. Rules for undeclared, unknown, or non-owned aliases
are invalid. The corpus pins `taxonomy_version` exactly to the taxonomy version
and includes both positive and forbidden context for every declared ambiguous
alias.

Validate the checked-in taxonomy and corpus without settings, database, or
network I/O:

```bash
uv run career-os taxonomy validate
```

Task 19 validates schemas, references, and corpus coverage only. It does not
extract or match skills, enrich jobs, or persist taxonomy data; task 20 owns
those behaviours and will execute the golden extraction expectations.

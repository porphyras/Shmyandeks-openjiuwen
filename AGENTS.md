# Working in this repository

Start with [the knowledge index](docs/knowledge/index.md), then read only the
topic and source files relevant to the change. Do not load all documentation.
The [repo-knowledge skill](.agents/skills/repo-knowledge/SKILL.md) provides local
search and maintenance commands; it adapts Google's `kb-search` example.

```sh
python3 tools/knowledge.py search budget
python3 tools/knowledge.py read budget-and-routing
python3 tools/knowledge.py check
```

## Sources of truth

- Current behavior: code and tests in the checked-out branch. A proposal or PR
  does not describe deployed behavior until its implementation is present.
- Intended changes: the relevant change spec or PR's explicit behavior contract.
- Run/build commands: README; bench format/grading: `bench/README_sets.md`.
- Historical decisions and reported experiments: `docs/DECISIONS.md`. Preserve
  their scope and limitations; a recorded score is not a new independent result.
- Knowledge pages are navigation and explanations backed by source fingerprints,
  not copies of every configuration value or independently verified truth.

## Making changes

For a behavioral change, state the problem, desired observable behavior and
verification before implementation. A short PR description is enough for a small
fix; use [the change-spec template](docs/templates/change-spec.md) for substantial
changes. Update the affected knowledge page when its contract changes. After
checking its prose against the sources, run `stamp <topic>`; never stamp merely
to silence drift. Regenerate navigation with `index --write` when metadata changes.

Offline checks need Python 3.11+ and no model credentials:

```sh
python3 -m unittest discover -s tests -v
python3 tools/knowledge.py check
```

For SDK, proxy or container changes, follow the relevant verification/environment
topic. Keep offline verification distinct from model-quality, billing and Docker
evidence. Keep credentials out of documentation and fixtures. Developer knowledge,
tools and specs are excluded from the submission image by `.dockerignore`.

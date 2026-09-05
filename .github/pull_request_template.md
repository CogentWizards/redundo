## What changed and why

<!-- The "why" matters more than the "what" here -- see CONTRIBUTING.md. -->

## Checklist

- [ ] `uv run pytest` passes locally
- [ ] New behavior has test coverage (a fixture, not just a real capture -- real captures are for validation, too brittle/large to be the actual test)
- [ ] If this touches an adapter source or the analyzer: coverage/known-gaps are still reported honestly, nothing silently guesses past an unknown
- [ ] Docs (`README.md`, `docs/*.md`) updated if this changes documented behavior

## Summary

<!-- What changed and why -->

## Checklist

- [ ] Tests pass (`make test`)
- [ ] Lint clean (`make lint`)
- [ ] New/changed behavior has test coverage
- [ ] SQL migration added if schema changed (`eos/migrations/00NN_*.sql`)
- [ ] Tenant isolation: queries/mutations include `studio_id`
- [ ] `.env.example` updated for new env vars
- [ ] Pushed to `main` after merge (or included in this PR)
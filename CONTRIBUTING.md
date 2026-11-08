# Contributing

Bug reports, fixes and new features are welcome. Every change reaches `main` through a pull request, and a pull request can be merged only after CI passes.

## Before you start

- For a bug, open an [issue](https://github.com/gogoymh/auto-ontology-mcp/issues) with the steps to reproduce it, what you expected and what happened. Include the tool name and its input if the bug is in an MCP tool.
- For a new tool, a new database dialect or a change to how the model is guided (`guidance.py`), open an issue first so we can agree on the approach before you write the code.
- Small fixes (typos, a failing edge case with a test) can go straight to a pull request.

## Set up and run the tests

```bash
git clone https://github.com/<your-account>/auto-ontology-mcp && cd auto-ontology-mcp
uv sync
uv run pytest
```

The PostgreSQL tests are skipped unless `AO_TEST_PG_URL` points at a disposable database. CI runs them against PostgreSQL 17, so run them locally when your change touches SQL generation, validation or the catalog:

```bash
docker run -d --rm --name aomcp-pg -e POSTGRES_PASSWORD=pw -p 55432:5432 postgres:17
AO_TEST_PG_URL=postgresql+psycopg://postgres:pw@127.0.0.1:55432/postgres uv run --extra postgres pytest
```

## Pull requests

1. Fork the repository and create a branch from `main`. Name it after the change (`fix-percent-warning`, not `patch-1`).
2. Keep one change per pull request. A bug fix and an unrelated refactor go in two pull requests.
3. Add or update tests in `tests/` for every behavior you change. A bug fix should come with a test that fails without the fix.
4. Update the README when a tool's input, output or behavior changes.
5. Leave the version alone. The maintainer bumps `pyproject.toml`, `__init__.py` and `uv.lock` when merging.
6. Fill in the pull request template: what changes, how you checked it, and what it still does not handle.

Pull requests are squash-merged, so the pull request title becomes the commit message on `main`. Write it as a sentence about the change (`Warn when a filter compares a percent column with a fraction`).

## Safety rules for changes

The server connects to other people's databases. A change must not:

- let `run_sql` or any probe write to a connected database. The SQL guard, the read-only connection and the rollback in [Safety](README.md#safety) all stay in place.
- send data anywhere other than the MCP client that asked for it.
- add a dependency that needs a model API key.

## License

By submitting a pull request, you agree that your contribution is licensed under the [Apache License 2.0](LICENSE), as section 5 of the license describes.

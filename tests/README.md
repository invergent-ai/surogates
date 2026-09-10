# Functional tests

Keep tests that exercise observable product behavior through API routes,
tool execution, agent or channel workflows, persistence, and real process
or sandbox operations. External services such as LLMs and messaging providers
may use test doubles; the functionality under test must run real application
code and assert its result or side effects.

Do not add isolated helper or model unit tests, default-value checks,
registration inventories, import or attribute existence checks, source-text
assertions, or tests that only repeat their own fixtures.

Run the default suite from the repository root:

```sh
uv run pytest tests
```

Tests under `integration/` use PostgreSQL and Redis through testcontainers
and require Docker. Live LLM and browser tests remain opt-in via the `live`,
`browser_e2e`, and `browser_e2e_k8s` markers. Individual sandbox tests document
their additional environment requirements.

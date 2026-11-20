"""What an MCP tool answers reaches the model with its code as written, and its secrets still masked.

Every MCP result and error passes ``_sanitize_error`` (the proxy's routes and pool, the client).  Its pattern
matched ``key=`` / ``token=`` anywhere, case-blind, and ``sk-`` inside words: on 2026-11-20, 46 coding attempts'
tool results held 956 ``[REDACTED]`` (``sorted(rows, [REDACTED] r: r[1])``), and the model hex-dumped its own
file to read it back.
"""

from __future__ import annotations

import pytest

from surogates.tools.mcp.client import _sanitize_error

CODE = [
    "sorted(items, key=lambda x: x[1])",
    "max(d, key=d.get)",
    "dict(key=value)",
    "rows.sort(key=itemgetter(1))",
    "monkey=banana",
    "task-1234567890abcdefgh",
    "risk-free disk-usage",
    "token=None",
    "token=self.token",
    "next_page_token=page_token",
    "max_token=5",
    'password=os.environ["PW"]',
    "password=get_secret()",
    'api_key=settings["k"]',
    'headers = {"Authorization": f"Bearer {token}"}',
    "Bearer token authentication",
    'cache_key=f"{user}:{day}"',
    "export TOKEN=$GITHUB_TOKEN",
    "OpenAI(api_key=api_key)",
    "connect(password=password)",
    "def login(user, password=DEFAULT_PASSWORD):",
    "secret_key=SECRET_KEY",
    "self.secret=secret",
    "if password==expected:",
    "key=public_key_pem_2048",
    "key=column_name_2024",
    "WWW-Authenticate: Bearer realm=api.example.com",
    'payload = {"password": "<your password>"}',
    "url = f\"https://api.x.io/v1?api_key={api_key}\"",
    "bypass=all",
    "n_pass=2",
    '{"compass": "north"}',
    "PWD=/home/u/proj OLDPWD=/tmp",
    "secret_bytes=secret_bytes_of(user)",
]

SECRETS = [
    ("ghp_" + "a1" * 18, "[REDACTED]"),
    ("github_pat_" + "Ab1" * 10, "[REDACTED]"),
    ("sk-" + "Ab1" * 16, "[REDACTED]"),
    ("sk-proj-" + "Ab1" * 16, "[REDACTED]"),
    ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.e30.abc", "Authorization: Bearer [REDACTED]"),
    ("Bearer abc123def456", "Bearer [REDACTED]"),
    ("password=hunter2", "password=[REDACTED]"),
    ("PASSWORD=hunter2", "PASSWORD=[REDACTED]"),
    ("db_password=letmein; host=x", "db_password=[REDACTED]; host=x"),
    ("connect(password=hunter2)", "connect(password=[REDACTED])"),
    ("{secret=s3cr3t}", "{secret=[REDACTED]}"),
    ("client_secret=xyz", "client_secret=[REDACTED]"),
    ("api_key=abcd1234efgh5678", "api_key=[REDACTED]"),
    ("x-api-key=short1", "x-api-key=[REDACTED]"),
    ("OPENAI_API_KEY=sk-" + "x1" * 20, "OPENAI_API_KEY=[REDACTED]"),
    ("https://x.io/a?token=abc123def456ghi789&page=2", "https://x.io/a?token=[REDACTED]&page=2"),
    ("key=AIzaSyA1234567890abcdefghij", "key=[REDACTED]"),
    ("token=ghp_" + "b2" * 18, "token=[REDACTED]"),
    ("access_token=ya29.a0AfH6SMB", "access_token=[REDACTED]"),
    # Spellings and values the first narrowing let through (review): camelCase, run-together, prefixed names.
    ("accessToken=abc123", "accessToken=[REDACTED]"),
    ("refreshToken=r3fr3sh", "refreshToken=[REDACTED]"),
    ("clientSecret=xyz", "clientSecret=[REDACTED]"),
    ("PGPASSWORD=s3cret", "PGPASSWORD=[REDACTED]"),
    ("mysecret=s3cret", "mysecret=[REDACTED]"),
    ("csrftoken=abcdef1234567890ghij", "csrftoken=[REDACTED]"),
    ("password=CorrectHorse.BatteryStaple", "password=[REDACTED]"),
    ("password=$ecret!", "password=[REDACTED]"),
    ("password=P@ss(word)1", "password=[REDACTED]"),
    ("password=pa[ss]1", "password=[REDACTED]"),
    ("password={hunter2}", "password=[REDACTED]"),
    ("sk-" + "q" * 300, "[REDACTED]"),
    ("Bearer abcdefghijklmnopqrstuvwxyzABCDEF", "Bearer [REDACTED]"),
    # Formats the old pattern never masked either.
    ('{"password": "hunter2"}', '{"password": "[REDACTED]"}'),
    ('{"api_key": "abcd"}', '{"api_key": "[REDACTED]"}'),
    ("postgres://app:s3cr3t@db:5432/x", "postgres://app:[REDACTED]@db:5432/x"),
    ("AKIAIOSFODNN7EXAMPLE", "[REDACTED]"),
    ("xoxb-1234567890-abcdefghij", "[REDACTED]"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig1234567890abc", "[REDACTED]"),
    # Weak values of .env files and compose files (review, round 2): names of secret words are literals here.
    ("password=password123", "password=[REDACTED]"),
    ("DB_PASSWORD=Passw0rd", "DB_PASSWORD=[REDACTED]"),
    ("ADMIN_PASSWORD=MyPassword", "ADMIN_PASSWORD=[REDACTED]"),
    ("JWT_SECRET=mysecretkey", "JWT_SECRET=[REDACTED]"),
    ("SECRET_KEY=supersecret", "SECRET_KEY=[REDACTED]"),
    ("API_KEY=testkey", "API_KEY=[REDACTED]"),
    ("POSTGRES_PASSWORD=secret", "POSTGRES_PASSWORD=[REDACTED]"),
    ("password=SECRET_PASSWORD_2024", "password=[REDACTED]"),
    ("password=HUNTER_TWO", "password=[REDACTED]"),
    ("DIGITALOCEAN_TOKEN=dop_v1_" + "a1" * 32, "DIGITALOCEAN_TOKEN=[REDACTED]"),
    ("SHOPIFY_TOKEN=shpat_" + "f0" * 16, "SHOPIFY_TOKEN=[REDACTED]"),
    ("API_TOKEN=a1b2c3d4_e5f6a7b8c9d0e1f2", "API_TOKEN=[REDACTED]"),
    ("redis://:p4ss@cache:6379/0", "redis://:[REDACTED]@cache:6379/0"),
    ('{"private_key": "-----BEGIN PRIVATE KEY-----\\n' + "A" * 1700 + '\\n-----END PRIVATE KEY-----\\n"}',
     '{"private_key": "[REDACTED]"}'),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----", "[REDACTED PRIVATE KEY]"),
    ("SECRET_KEY=my_secret_key", "SECRET_KEY=[REDACTED]"),
    ("API_KEY=testKey", "API_KEY=[REDACTED]"),
    ("API_TOKEN=abc123short", "API_TOKEN=[REDACTED]"),
    ('{"api_token": "9f8e7d6c"}', '{"api_token": "[REDACTED]"}'),
    ("DB_PASS=123456", "DB_PASS=[REDACTED]"),
]


@pytest.mark.parametrize("text", CODE)
def test_code_and_prose_reach_the_model_as_written(text):
    assert _sanitize_error(text) == text


@pytest.mark.parametrize(("text", "masked"), SECRETS)
def test_secrets_are_still_masked(text, masked):
    assert _sanitize_error(text) == masked


def test_a_file_read_through_an_mcp_tool_keeps_its_code():
    source = (
        "def top(rows, n):\n"
        "    ranked = sorted(rows, key=lambda r: r['score'], reverse=True)\n"
        "    return [r for r in ranked[:n] if r.get('token') != token]\n"
        "client = Client(api_key=os.environ['API_KEY'], timeout=30)\n"
    )
    assert _sanitize_error(source) == source


def test_a_secret_in_the_middle_of_a_result_is_masked_and_the_rest_kept():
    text = "connected with OPENAI_API_KEY=sk-" + "Zz9" * 12 + " in 0.3 s; risk-free mode, key=lambda kept"
    assert _sanitize_error(text) == "connected with OPENAI_API_KEY=[REDACTED] in 0.3 s; risk-free mode, key=lambda kept"


def test_the_patterns_stay_linear_on_long_hostile_input():
    """A tool result can be megabytes: nothing here may backtrack on it."""
    import time

    for hostile in ("a" * 200_000, "password=" + "a(" * 50_000, '"secret": "' + "\\" * 100_000, "Bearer " + "a" * 200_000):
        started = time.monotonic()
        _sanitize_error(hostile)
        assert time.monotonic() - started < 2.0


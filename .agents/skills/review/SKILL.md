# Publication review

Run `uv run ruff check .`, `uv run ruff format --check .`,
`uv run pytest --cov=agent_mail_cli --cov-report=term-missing`,
`uv run bandit -q -r src`, and `uv build` locally.
Review the producer-to-consumer flow: S3 provenance, MIME, target validation,
review hash, conditional claim, one SES attempt, durable result and uncertainty.
Scan all tracked files and Git history for secrets and real deployment data.
CI repeats lint, tests, security review and package build without AWS credentials.

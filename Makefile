# Ouroboros — common development commands
# Usage: make test, make lint, make health

.PHONY: test lint health clean

# Run smoke tests (fast, no external deps needed at runtime)
test:
	python3 -m pytest tests/ -q --tb=short

# Run smoke tests with verbose output
test-v:
	python3 -m pytest tests/ -v --tb=long

# Lint: deterministic F-rule gate (NameError class); matches the CI quick-test step
lint:
	python3 -m ruff check . --select F

# Run codebase health check (requires ouroboros importable)
health:
	python3 -c "from ouroboros.review import collect_sections, compute_complexity_metrics; \
		import pathlib, json; \
		sections, stats = collect_sections(pathlib.Path('.'), pathlib.Path('../data')); \
		m = compute_complexity_metrics(sections); \
		print(json.dumps({'repo': stats, **m}, indent=2, default=str))"

# Clean Python cache files
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true

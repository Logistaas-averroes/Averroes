# Logistaas Ads Intelligence System — Manual Ops Runner
# Usage: make <target>
#
# Targets:
#   make healthcheck   — validate env vars and runtime dependencies
#   make daily         — run the daily pulse scheduler
#   make weekly        — run the weekly report scheduler
#   make monthly       — run the monthly report scheduler
#   make runs          — tail the 20 most recent run-history records
#
# Rules:
#   - Convenience wrapper only.  No business logic.
#   - All entry points are standard Python modules.
#   - No hardcoded secrets or paths.
#   - Commands fail clearly when required env vars are missing.

.PHONY: healthcheck daily weekly monthly validate runs help

# Default target
help:
	@echo ""
	@echo "Logistaas Ads Intelligence — Manual Ops Runner"
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo "  make healthcheck   Validate env vars + runtime dependencies"
	@echo "  make daily         Run the daily pulse scheduler"
	@echo "  make weekly        Run the weekly report scheduler"
	@echo "  make monthly       Run the monthly report scheduler"
	@echo "  make validate      Run Phase 1 end-to-end validation"
	@echo "  make runs          Show recent run history (last 20 records)"
	@echo ""

healthcheck:
	python scripts/healthcheck.py

daily:
	python -m scheduler.daily

weekly:
	python -m scheduler.weekly

monthly:
	python -m scheduler.monthly

validate:
	python scripts/validate_phase1.py

runs:
	@if [ -f runtime_logs/run_history.jsonl ]; then \
		echo ""; \
		echo "Recent run history (last 20 records):"; \
		echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"; \
		tail -n 20 runtime_logs/run_history.jsonl | python -c \
			"import sys, json; [print(json.dumps(json.loads(l), indent=2)) for l in sys.stdin]"; \
	else \
		echo "No run history found at runtime_logs/run_history.jsonl"; \
	fi

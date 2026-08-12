#!/usr/bin/env bash
# Runs the CI "Frappe Linter" and "Code Quality" jobs.
# Each tool reports on its own and the script fails if any of them failed.

set -Eeuo pipefail

# shellcheck source=lib-checkout.sh
source /usr/local/bin/lib-checkout.sh

materialise_checkout "${HOME}/src"

STATUS=0

run() {
	local name="$1"
	shift
	echo
	echo "=== ${name} ==="
	if "$@"; then
		echo "${name}: pass"
	else
		echo "${name}: FAIL"
		STATUS=1
	fi
}

# Hook environments are built on first run and cached in the named volume.
run pre-commit env SKIP=no-commit-to-branch pre-commit run --all-files --show-diff-on-failure

run isort isort --check-only --diff .
run black black --check --diff .
run flake8 flake8 .
# ignored-modules in pyproject.toml covers the bench packages.
run pylint pylint --rcfile=pyproject.toml frappe_paystack
run mypy mypy --config-file=pyproject.toml frappe_paystack

echo
if [ "${STATUS}" -ne 0 ]; then
	echo "Quality checks failed."
else
	echo "Quality checks passed."
fi
exit "${STATUS}"

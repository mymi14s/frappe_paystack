#!/usr/bin/env bash
# Runs the frappe_paystack suite under coverage and gates on both results.
#
# Usage: run-tests-with-coverage.sh <bench-dir> <site>
#
# Exits non-zero when any test fails, when no test summary is produced, or when
# coverage falls below the fail_under in .coveragerc.

set -Eeuo pipefail

BENCH_DIR="${1:?bench directory required}"
SITE="${2:?site name required}"

APP_DIR="${BENCH_DIR}/apps/frappe_paystack"
RCFILE="${APP_DIR}/.coveragerc"
PYTHON="${BENCH_DIR}/env/bin/python"
TEST_LOG="$(mktemp)"

trap 'rm -f "${TEST_LOG}"' EXIT

cd "${BENCH_DIR}/sites"

# Coverage attaches before frappe boots, and the report reads the data file this
# run writes, so both run against one data file from this directory.
set +e
"${PYTHON}" -m coverage run --rcfile="${RCFILE}" \
	-m frappe.utils.bench_helper frappe --site "${SITE}" run-tests --app frappe_paystack \
	2>&1 | tee "${TEST_LOG}"
set -e

# bench run-tests exits 0 even when tests fail, so the summary decides the result.
if ! grep -qE '^Ran [0-9]+ test' "${TEST_LOG}"; then
	echo "::error::No test summary found. The suite did not run to completion."
	exit 1
fi

RAN_LINE="$(grep -E '^Ran [0-9]+ test' "${TEST_LOG}" | tail -1)"
echo "${RAN_LINE}"

if grep -qE '^Ran 0 tests' "${TEST_LOG}"; then
	echo "::error::The suite collected 0 tests."
	exit 1
fi

RESULT_LINE="$(grep -E '^(OK|FAILED)' "${TEST_LOG}" | tail -1 || true)"

if [ -z "${RESULT_LINE}" ]; then
	echo "::error::No OK/FAILED line found after the test summary."
	exit 1
fi

if [[ "${RESULT_LINE}" == FAILED* ]]; then
	echo "::error::Tests failed: ${RESULT_LINE}"
	grep -E '^(FAIL|ERROR):' "${TEST_LOG}" || true
	TESTS_FAILED=1
else
	echo "Tests passed: ${RESULT_LINE}"
	TESTS_FAILED=0
fi

# The report reads the data file the run above wrote; a stale file misattributes
# lines, so the report always follows its own run.
echo "--- Coverage ---"
set +e
"${PYTHON}" -m coverage report --rcfile="${RCFILE}" --fail-under=100
COVERAGE_FAILED=$?
set -e

if [ "${COVERAGE_FAILED}" -ne 0 ]; then
	echo "::error::Coverage is below the required threshold."
fi

if [ "${TESTS_FAILED}" -ne 0 ] || [ "${COVERAGE_FAILED}" -ne 0 ]; then
	exit 1
fi

echo "Tests and coverage both pass."

#!/usr/bin/env bash
# Runs the CI "Semgrep Rules" and "Vulnerable Dependency Check" jobs.

set -Eeuo pipefail

# shellcheck source=lib-checkout.sh
source /usr/local/bin/lib-checkout.sh

SRC="${HOME}/src"

materialise_checkout "${SRC}"

STATUS=0

echo
echo "=== Semgrep Rules ==="
git clone --depth 1 --quiet https://github.com/frappe/semgrep-rules.git "${HOME}/frappe-semgrep-rules"

# The workflow runs `semgrep ci`, which reports findings new since the merge base.
# This scans the whole tree, so it also lists findings that predate the branch.
if semgrep scan \
	--config "${HOME}/frappe-semgrep-rules/rules" \
	--config r/python.lang.correctness \
	--error \
	--metrics off \
	"${SRC}"; then
	echo "semgrep: pass"
else
	echo "semgrep: FAIL"
	STATUS=1
fi

echo
echo "=== Vulnerable Dependency Check ==="
cd "${SRC}"
if pip-audit --desc on .; then
	echo "pip-audit: pass"
else
	echo "pip-audit: FAIL"
	STATUS=1
fi

echo
if [ "${STATUS}" -ne 0 ]; then
	echo "Security checks failed."
else
	echo "Security checks passed."
fi
exit "${STATUS}"

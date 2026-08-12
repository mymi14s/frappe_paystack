#!/usr/bin/env bash
# Runs the CI "Server" job: builds a bench, creates a site, runs the suite under
# coverage. Mirrors the steps in .github/workflows/ci.yml.

set -Eeuo pipefail

# shellcheck source=lib-checkout.sh
source /usr/local/bin/lib-checkout.sh

: "${PYTHON_VERSION:?PYTHON_VERSION required}"
: "${FRAPPE_BRANCH:?FRAPPE_BRANCH required}"
: "${ERPNEXT_BRANCH:?ERPNEXT_BRANCH required}"
: "${PAYMENTS_BRANCH:?PAYMENTS_BRANCH required}"
: "${WEBSHOP_BRANCH:?WEBSHOP_BRANCH required}"
: "${COVERAGE_VERSION:?COVERAGE_VERSION required}"

# bench names the app after this directory.
SRC="${HOME}/checkout/frappe_paystack"
BENCH_DIR="${HOME}/frappe-bench"
SITE="${SITE_NAME:-test_site}"
DB_HOST="${DB_HOST:-mariadb}"
DB_PORT="${DB_PORT:-3306}"
REDIS_CACHE="${REDIS_CACHE:-redis://redis-cache:6379}"
REDIS_QUEUE="${REDIS_QUEUE:-redis://redis-queue:6379}"

step() {
	echo
	echo "=== $* ==="
}

step "Clone"
materialise_checkout "${SRC}"

step "Check for merge conflicts and syntax errors"
python -m compileall -q -f "${SRC}/frappe_paystack"
if grep -lr --exclude-dir=node_modules --exclude-dir=.git "^<<<<<<< " "${SRC}"; then
	echo "Found merge conflict markers"
	exit 1
fi

step "Setup bench"
bench init \
	--skip-redis-config-generation \
	--skip-assets \
	--frappe-branch "${FRAPPE_BRANCH}" \
	--python "$(command -v python)" \
	"${BENCH_DIR}" < /dev/null

cd "${BENCH_DIR}"

# GitHub publishes the service containers on localhost. Compose resolves them by
# service name, so the generated config is repointed.
bench set-config -g db_host "${DB_HOST}"
bench set-config -g db_port "${DB_PORT}" --parse
bench set-config -g redis_cache "${REDIS_CACHE}"
bench set-config -g redis_queue "${REDIS_QUEUE}"
bench set-config -g redis_socketio "${REDIS_QUEUE}"

mariadb --host "${DB_HOST}" --port "${DB_PORT}" -u root -proot \
	-e "SET GLOBAL character_set_server = 'utf8mb4'"
mariadb --host "${DB_HOST}" --port "${DB_PORT}" -u root -proot \
	-e "SET GLOBAL collation_server = 'utf8mb4_unicode_ci'"

step "Check the bench interpreter"
./env/bin/python -VV
FOUND="$(./env/bin/python -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "bench venv runs Python ${FOUND}, compose asked for ${PYTHON_VERSION}"
if [ "${FOUND}" != "${PYTHON_VERSION}" ]; then
	echo "bench built its venv on Python ${FOUND}, not ${PYTHON_VERSION}"
	exit 1
fi

step "Install apps"
bench get-app https://github.com/frappe/erpnext --branch "${ERPNEXT_BRANCH}" --resolve-deps
# frappe_paystack imports payments.utils.
bench get-app https://github.com/frappe/payments --branch "${PAYMENTS_BRANCH}"
# The storefront the webshop and portal suites run against.
bench get-app https://github.com/frappe/webshop --branch "${WEBSHOP_BRANCH}"
bench get-app frappe_paystack "${SRC}" --branch ci-run
bench setup requirements --dev

# frappe's dev dependencies pin coverage below this line on both branches, and
# their branch analysis differs; this wins.
./env/bin/pip install --upgrade "coverage==${COVERAGE_VERSION}"
FOUND="$(./env/bin/python -c 'import coverage; print(coverage.__version__)')"
echo "bench venv runs coverage ${FOUND}"
if [ "${FOUND}" != "${COVERAGE_VERSION}" ]; then
	echo "coverage ${FOUND} is installed, not ${COVERAGE_VERSION}"
	exit 1
fi

step "Create site"
# GitHub starts on an empty database; the compose volume keeps the last run's site.
NEW_SITE_ARGS=(--force --set-default --db-root-password root --admin-password admin)
# The site user is created for the container's address, not localhost.
if bench new-site --help 2>&1 | grep -q -- "--mariadb-user-host-login-scope"; then
	NEW_SITE_ARGS+=(--mariadb-user-host-login-scope='%')
fi
bench new-site "${NEW_SITE_ARGS[@]}" "${SITE}"

bench --site "${SITE}" install-app erpnext
bench --site "${SITE}" install-app payments
bench --site "${SITE}" install-app webshop
bench --site "${SITE}" install-app frappe_paystack
bench --site "${SITE}" set-config allow_tests true
bench build

step "Run Tests"
bash "${BENCH_DIR}/apps/frappe_paystack/.github/scripts/run-tests-with-coverage.sh" \
	"${BENCH_DIR}" "${SITE}"

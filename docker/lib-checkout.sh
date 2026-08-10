#!/usr/bin/env bash
# Copies the mounted checkout into the container.
#
# GitHub tests the pushed commit. This tests the working tree, so uncommitted and
# untracked files are carried across and committed onto a local branch.

WORKSPACE="${WORKSPACE:-/workspace}"

materialise_checkout() {
	local dest="$1"
	local branch="${2:-ci-run}"

	# The mount is owned by the host user, which need not match the container user.
	git config --global --add safe.directory '*'

	rm -rf "${dest}"
	git clone --no-hardlinks --quiet "${WORKSPACE}" "${dest}"

	cd "${dest}"
	git checkout -qB "${branch}"

	# bench get-app parses the origin url as org/repo/tag, and a local path has
	# neither. Without a remote it reads the directory name instead.
	git remote remove origin

	# ls-files honours .gitignore, so node_modules and caches stay out.
	(cd "${WORKSPACE}" && git ls-files -z --cached --others --exclude-standard) |
		tar -C "${WORKSPACE}" --null -T - -cf - |
		tar -C "${dest}" -xf -

	git add -A
	if git diff --cached --quiet; then
		echo "Checkout matches HEAD ($(git rev-parse --short HEAD))."
	else
		git -c user.email=ci@local -c user.name=CI commit -qm "local working tree"
		echo "Uncommitted work committed onto ${branch}."
	fi
}

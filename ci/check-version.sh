#!/usr/bin/env bash
# Assert the release tag matches the packaged version, so a `vX.Y.Z` release can never
# attach a wheel built from a different version than the tag claims. Called with the tag
# as $1 (e.g. "v0.1.0"). Dependency-free (no jq/uv required).
#
# The version is NOT in pyproject.toml here: `dynamic = ["version"]` plus
# `[tool.hatch.version] path = "vgi_typesafe/__init__.py"` makes __init__.py the single
# source. The fleet's copy of this script greps pyproject.toml, which in this repo would
# match nothing and pass on an empty string — hence the explicit empty check below.
set -euo pipefail

tag="${1:?usage: check-version.sh <tag>}"
want="${tag#v}" # strip a leading 'v'
have="$(sed -nE 's/^__version__ = "([^"]+)".*/\1/p' vgi_typesafe/__init__.py)"

if [ -z "$have" ]; then
  echo "Could not read __version__ from vgi_typesafe/__init__.py" >&2
  exit 1
fi

if [ "$want" != "$have" ]; then
  echo "Version mismatch: tag ${tag} (-> ${want}) != vgi_typesafe/__init__.py ${have}" >&2
  exit 1
fi
echo "Version OK: vgi_typesafe/__init__.py ${have} matches tag ${tag}"

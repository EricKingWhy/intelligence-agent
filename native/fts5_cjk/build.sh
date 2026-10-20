#!/usr/bin/env bash
# Build libfts5_cjk.so into this directory (or $1).
#
# Runtime lookup (session_archive.py): <workspace>/sessions/libfts5_cjk.so
# next to the archive db, overridable with OWLET_FTS5_CJK_SO.
#
# Uses the system sqlite3ext.h when present, else the vendored copy in
# vendor/ (public-domain SQLite amalgamation headers) so the build works
# without libsqlite3-dev installed.
set -euo pipefail
cd "$(dirname "$0")"

CFLAGS_EXTRA=""
if ! echo '#include <sqlite3ext.h>' | gcc -E -xc - >/dev/null 2>&1; then
  CFLAGS_EXTRA="-Ivendor"
fi

gcc -shared -fPIC -O2 -Wall -Wextra $CFLAGS_EXTRA fts5_cjk.c -o libfts5_cjk.so
dest="${1:-$(pwd)}"
if [ "$dest" != "$(pwd)" ]; then
  mkdir -p "$dest"
  install -m 0644 libfts5_cjk.so "$dest/libfts5_cjk.so"
fi
echo "built: $dest/libfts5_cjk.so"

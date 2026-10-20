# fts5_cjk — cjk_unicode61 FTS5 tokenizer

unicode61 + CJK character bigrams (Lucene CJKAnalyzer semantics). Fixes
1-2 char Korean/Chinese/Japanese terms falling through to LIKE full-table
scans in session search.

Build:

    ./build.sh

The `.so` lands next to the source (`native/fts5_cjk/libfts5_cjk.so`).
Pass a directory to install elsewhere: `./build.sh /path/to/dir`.

Uses the system `sqlite3ext.h` when available, else the vendored copy in
`vendor/` — no libsqlite3-dev required.

## Loading

At open time the session archive looks for `libfts5_cjk.so` in the archive
directory (default `<workspace>/sessions/libfts5_cjk.so`, i.e. next to the
db). To keep the `.so` anywhere else, set:

    OWLET_FTS5_CJK_SO=/abs/path/libfts5_cjk.so

Copy the built file into place (`<workspace>/sessions/` is the default
`workspace_dir`'s session directory):

    cp native/fts5_cjk/libfts5_cjk.so .agent/workspace/sessions/

The next archive open creates the `messages_fts_cjk` index
(external-content, tool rows excluded — same storage discipline as the
other indexes). A populated database is backfilled automatically on that
open, in one transaction; new messages are indexed live by triggers
afterwards.

## Disabling / degradation

Not placing the `.so` is the off switch — no config needed. Without it the
archive degrades fail-open: CJK triggers are dropped so writes keep
working, a stale marker records that the index has an unknown gap, and
search falls back to the trigram index / LIKE. Put the `.so` back and the
next open rebuilds the CJK index from scratch and clears the marker.

Contributed by Soju06 (PR #65544).

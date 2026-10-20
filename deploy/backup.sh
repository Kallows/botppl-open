#!/bin/sh
# Nightly SQLite backup, 7 rotating weekday slots. Fails loudly into the journal.
# Backups hold vault bodies and emails: root-only files, in a directory the
# botppl service user cannot write (its unit grants ReadWritePaths only to
# /var/lib/botppl), so a compromised app process cannot read or delete them.
set -eu
umask 077
DB=/var/lib/botppl/botppl.db
DIR=/var/backups/botppl
mkdir -p "$DIR"
chmod 700 "$DIR"
OUT=$DIR/backup-$(date +%u).db
sqlite3 "$DB" ".backup '$OUT'"
chmod 600 "$OUT"
# verify the backup actually opens and has our tables
sqlite3 "$OUT" "SELECT count(*) FROM messages;" > /dev/null
echo "botppl backup ok: $OUT ($(stat -c%s "$OUT") bytes)"

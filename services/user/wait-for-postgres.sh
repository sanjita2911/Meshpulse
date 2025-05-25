#!/bin/sh
set -e

host="$1"
shift
cmd="$@"

until PGPASSWORD=pass psql -h "$host" -U "user" -d "users_db" -c '\q' 2>/dev/null; do
  >&2 echo "Postgres is unavailable - sleeping"
  sleep 1
done

>&2 echo "Postgres is up - executing command"
exec $cmd

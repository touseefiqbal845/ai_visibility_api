#!/usr/bin/env sh
set -e

# Postgres accepts connections a moment after the container reports healthy.
echo "applying migrations..."
flask db upgrade

echo "starting server..."
exec gunicorn --bind 0.0.0.0:5000 --workers 2 --timeout 180 "wsgi:app"

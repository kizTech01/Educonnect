#!/bin/sh

# Fail the container startup if either schema or static-file preparation fails.
# This prevents Gunicorn from serving an application against a stale schema.
set -e

echo "Running database migrations..."
python manage.py migrate --noinput

echo "Collecting static files..."
python manage.py collectstatic --noinput

echo "Starting application server..."
exec "$@"

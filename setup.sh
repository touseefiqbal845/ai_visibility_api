#!/usr/bin/env bash
# Local setup without Docker. Assumes Python 3.11+.
set -e

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example - add your API keys before running the pipeline."
fi

python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

export FLASK_APP=wsgi.py
flask db upgrade

echo
echo "Setup complete. Start the server with:"
echo "  . .venv/bin/activate && FLASK_APP=wsgi.py flask run --port 5000"

#!/usr/bin/env bash
# Build the frontend and copy to static/ so one Render Web Service serves both API and UI.
# Run from repo root. Requires Node/npm.
set -e
cd "$(dirname "$0")/../frontend"
npm install
# Empty VITE_API_BASE_URL = same origin (API at /api)
VITE_API_BASE_URL= npm run build
rm -rf ../static
cp -r dist ../static
echo "Done. static/ updated. Commit and push to deploy on Render."

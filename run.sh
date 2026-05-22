#!/bin/bash
set -e
echo "Installing dependencies..."
pip install -r requirements.txt

echo ""
echo "Starting Frazil Roadmap server..."
echo "Open: http://localhost:8000"
echo ""
python server.py

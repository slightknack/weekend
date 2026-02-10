#!/bin/bash
set -e
cd "$(dirname "$0")"
python3 -m venv venv
source venv/bin/activate
pip install flask fast-flights airportsdata
echo ""
echo "Setup complete. Run:"
echo "  source venv/bin/activate"
echo "  python app.py"

#!/usr/bin/env bash
cd "$(dirname "$0")"
[ -f demo_data/motabaa_demo.db ] || python seed_demo.py --reset
exec python -m streamlit run app.py

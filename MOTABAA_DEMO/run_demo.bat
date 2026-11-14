@echo off
cd /d "%~dp0"
if not exist demo_data\motabaa_demo.db python seed_demo.py --reset
python -m streamlit run app.py

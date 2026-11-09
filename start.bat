@echo off
REM Start the Network Traffic Congestion Visualizer (Windows)
cd /d "%~dp0"
python -m pip install -r requirements.txt
python -m streamlit run app.py
pause

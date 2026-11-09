#!/usr/bin/env bash
# Start the Network Traffic Congestion Visualizer (Linux / macOS)
cd "$(dirname "$0")"
python3 -m pip install -r requirements.txt
python3 -m streamlit run app.py

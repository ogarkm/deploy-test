#!/bin/bash
# Install system dependencies needed for FPDF/Pillow if necessary
# Render usually has these, but just in case:
# apt-get update && apt-get install -y libgl1

# Start the application using uvicorn
# We use the $PORT variable provided by Render
uvicorn app:app --host 0.0.0.0 --port $PORT
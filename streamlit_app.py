"""Streamlit entrypoint — delegates to the UI package."""
import sys
import os

from dotenv import load_dotenv

# Load local .env into process environment before any modules import
load_dotenv()

# Ensure src is on path when running directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from scholarship_finder.ui.app import main

main()

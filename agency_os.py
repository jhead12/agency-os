#!/usr/bin/env python3
"""agency-os entry point — run with: python agency_os.py <command>"""

import sys
import os

# Ensure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.cli import cli

if __name__ == "__main__":
    cli()
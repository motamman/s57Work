#!/usr/bin/env python3
"""Build the navigation mesh of a finished chart file. See mesh/cli.py
and docs/MESH.md. Needs the packages in mesh/requirements.txt."""
import sys

from mesh.cli import main

if __name__ == "__main__":
    sys.exit(main())

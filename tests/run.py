#!/usr/bin/env python3
"""
Run all e2e tests for the Kilo Code MCP Memory server.

Usage:
    python tests/run.py
    python tests/run.py -v        # verbose
    python tests/run.py TestList  # run only test_list_projects
"""

import os
import sys

# Ensure the project root is on sys.path so 'from models import ...' works
# regardless of whether this script is run from the project root or elsewhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.discover("tests", pattern="test_*.py")

    runner = unittest.TextTestRunner(
        verbosity=2 if "-v" in sys.argv else 1,
        failfast=False,
    )
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)

"""Single source of truth for the version.

Kept out of __init__.py so cli.py can read it without importing the package
root, which imports cli.py in turn.
"""

__version__ = "0.4.0"

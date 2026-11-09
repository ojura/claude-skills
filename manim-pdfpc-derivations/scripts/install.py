#!/usr/bin/env python3
"""Install the canonical committed derivation toolkit into a project."""
import sys

sys.dont_write_bytecode = True
from derivation_toolkit.install import main

raise SystemExit(main())

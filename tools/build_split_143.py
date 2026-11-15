#!/usr/bin/env python3
"""Compatibility entry point for the standalone 143 candidate builder."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from firmware.split_143 import *

if __name__ == "__main__":
    main()

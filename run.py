#!/usr/bin/env python3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / 'vendor'
if VENDOR.exists():
    sys.path.insert(0, str(VENDOR))

from news_admin.cli import main

if __name__ == '__main__':
    main()

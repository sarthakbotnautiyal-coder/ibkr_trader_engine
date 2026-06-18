#!/usr/bin/env python3
"""IBKR Trader Engine - SPX 0DTE auto-trader."""

import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

# Placeholder: Full engine.py from monolith to be integrated
print("🚀 IBKR Trader Engine")
print("Note: Full engine.py from monolith needs to be ported here")
print("This should import and run AutoTraderEngine with proper config management")

if __name__ == "__main__":
    def handle_sigterm(signum, frame):
        print("SIGTERM received, shutting down...")
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)
    
    try:
        print("Engine running (placeholder)")
        import time
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("Interrupted, shutting down...")

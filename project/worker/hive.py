"""Hive blockchain worker — entry point shim.

The implementation is split across focused modules:
  - classify.py: classification, sentiment, language detection
  - community.py: community → category mapping
  - stream.py: live blockchain stream processing
  - backfill.py: HAFSQL backfill thread
  - bridge.py: async DB bridge
  - main.py: orchestrator
"""
from .main import run

if __name__ == "__main__":
    run()

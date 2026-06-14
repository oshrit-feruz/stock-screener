#!/usr/bin/env python3
"""
Recovery Entry Detector v2 validation (Stage 5b).

Drops recovery_score (ablation showed it adds noise).
New weights: dip=50%, momentum=30%, volume=20%.
Saves to results/recovery_entry_v2.txt.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from run_recovery_validation import main

if __name__ == "__main__":
    main(out_filename="recovery_entry_v2.txt", label="Stage 5b (v2: no recovery_score)")

#!/usr/bin/env python3
"""Download missing GDPval reference files using curl (more reliable than hf_hub)."""

import subprocess
import sys
import time
from pathlib import Path
import pandas as pd
import urllib.parse

REPO_ROOT = Path(__file__).parent.resolve()
DATASET_DIR = REPO_ROOT / "gdpval_dataset"
PARQUET_PATH = DATASET_DIR / "data" / "train-00000-of-00001.parquet"

def main():
    df = pd.read_parquet(PARQUET_PATH)

    # Collect all unique reference files
    all_refs = set()
    for _, row in df.iterrows():
        refs = row["reference_files"]
        if hasattr(refs, "tolist"):
            refs = refs.tolist()
        for f in refs:
            all_refs.add(f)

    # Filter to missing
    missing = [f for f in sorted(all_refs) if not (DATASET_DIR / f).exists()]
    print(f"Missing reference files: {len(missing)} / {len(all_refs)}")

    if not missing:
        print("All reference files already present!")
        return

    base_url = "https://huggingface.co/datasets/openai/gdpval/resolve/main/"
    success = 0
    failed = 0

    for i, filepath in enumerate(missing, 1):
        local_path = DATASET_DIR / filepath
        local_path.parent.mkdir(parents=True, exist_ok=True)
        url = base_url + urllib.parse.quote(filepath, safe="/")

        for attempt in range(3):
            try:
                result = subprocess.run(
                    ["curl", "-s", "-L", "-o", str(local_path), url],
                    capture_output=True,
                    timeout=30,
                )
                if result.returncode == 0 and local_path.stat().st_size > 0:
                    success += 1
                    break
                else:
                    if attempt == 2:
                        failed += 1
                        print(f"  FAILED: {filepath}")
                    time.sleep(2)
            except Exception as e:
                if attempt == 2:
                    failed += 1
                    print(f"  FAILED: {filepath} -> {e}")
                time.sleep(2)

        if i % 20 == 0:
            print(f"  Progress: {i}/{len(missing)} ({success} ok, {failed} failed)")
        time.sleep(0.5)  # Be nice to HF servers

    print(f"\nDone: {success} downloaded, {failed} failed")

if __name__ == "__main__":
    main()

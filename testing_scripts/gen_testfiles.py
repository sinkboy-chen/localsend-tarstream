#!/usr/bin/env python3
"""Generate test file directories for the 602 FP experiment.

Creates 9 directories (data/1 through data/9), each containing files of a specific size:
  1-3: 1.024 KB files (10, 100, 1000 count)
  4-6: 10.24 KB files (10, 100, 1000 count)
  7-9: 102.4 KB files (10, 100, 1000 count)

Usage:
    python gen_testfiles.py [--output-dir DIR] [--only N]

    --output-dir DIR   Base directory for generated folders (default: data)
    --only N           Only generate scenario N (1 through 9)
"""

import argparse
import os
import sys
import time
from pathlib import Path

TOTAL_BYTES = 10 * 1024 * 1024 * 1024  # 10 GiB = 10,737,418,240 bytes

SCENARIOS = [
    # 1.024 KB configs
    {"name": "1", "count": 10, "size_bytes": 1024, "label": "10 x 1.024 KB"},
    {"name": "2", "count": 100, "size_bytes": 1024, "label": "100 x 1.024 KB"},
    {"name": "3", "count": 1000, "size_bytes": 1024, "label": "1000 x 1.024 KB"},
    
    # 10.24 KB configs
    {"name": "4", "count": 10, "size_bytes": 10240, "label": "10 x 10.24 KB"},
    {"name": "5", "count": 100, "size_bytes": 10240, "label": "100 x 10.24 KB"},
    {"name": "6", "count": 1000, "size_bytes": 10240, "label": "1000 x 10.24 KB"},
    
    # 102.4 KB configs
    {"name": "7", "count": 10, "size_bytes": 102400, "label": "10 x 102.4 KB"},
    {"name": "8", "count": 100, "size_bytes": 102400, "label": "100 x 102.4 KB"},
    {"name": "9", "count": 1000, "size_bytes": 102400, "label": "1000 x 102.4 KB"},
]


def generate_scenario(base_dir: Path, scenario: dict) -> None:
    name = scenario["name"]
    count = scenario["count"]
    size = scenario["size_bytes"]
    label = scenario["label"]

    target_dir = base_dir / name
    target_dir.mkdir(parents=True, exist_ok=True)

    existing = len(list(target_dir.iterdir()))
    if existing >= count:
        print(f"  [SKIP] {target_dir} already has {existing} files (need {count})")
        return

    total_bytes = count * size
    print(f"  Generating: {label}")
    print(f"  Directory:  {target_dir}")
    print(f"  File size:  {size:,} bytes")
    print(f"  Total size: {total_bytes / (1024**3):.2f} GB")
    print()

    # Pre-generate a random chunk to reuse (avoids slow os.urandom calls per file)
    # Use 1 MB chunk for large files, full file size for small files
    chunk_size = min(size, 1024 * 1024)
    chunk = os.urandom(chunk_size)

    start_time = time.time()
    last_print = start_time

    for i in range(existing, count):
        file_path = target_dir / f"testfile_{i:07d}.bin"
        with open(file_path, "wb") as f:
            remaining = size
            while remaining > 0:
                write_size = min(remaining, chunk_size)
                f.write(chunk[:write_size])
                remaining -= write_size

        now = time.time()
        if now - last_print >= 2.0 or i == count - 1:
            elapsed = now - start_time
            done = i - existing + 1
            total_to_do = count - existing
            speed = done / elapsed if elapsed > 0 else 0
            eta = (total_to_do - done) / speed if speed > 0 else 0
            print(
                f"  Progress: {done:,}/{total_to_do:,} files "
                f"({done / total_to_do * 100:.1f}%) "
                f"| {speed:.0f} files/s "
                f"| ETA: {eta:.0f}s",
                flush=True,
            )
            last_print = now

    elapsed = time.time() - start_time
    print(f"  Done in {elapsed:.1f}s")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate test file directories for 602 FP")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).parent.resolve() / "data",
        help="Base directory for generated folders",
    )
    parser.add_argument(
        "--only",
        type=int,
        choices=[1, 2, 3],
        default=None,
        help="Only generate scenario N (1=100x102.4MB, 2=10000x1.024KB, 3=1000000x10.24KB)",
    )
    args = parser.parse_args()

    base_dir = args.output_dir.resolve()
    if not base_dir.exists():
        print(f"Error: output directory does not exist: {base_dir}", file=sys.stderr)
        return 1

    print(f"Base directory: {base_dir}")
    print()

    scenarios = SCENARIOS if args.only is None else [SCENARIOS[args.only - 1]]

    for scenario in scenarios:
        generate_scenario(base_dir, scenario)

    print("=" * 60)
    print("All done! To use with LocalSend:")
    print()
    print("  In LocalSend, use 'Send folder' or drag the folder into")
    print("  the LocalSend window to send all files at once.")
    print()
    print("  Then update experiment.json:")
    print("    - file_path: point to any ONE file in the folder")
    print("    - file_count: set to the number of files")
    print()
    for s in scenarios:
        d = base_dir / s["name"]
        print(f"  {s['label']}:")
        print(f"    file_path: {d / 'testfile_0000000.bin'}")
        print(f"    file_count: {s['count']}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# LocalSend Batching Benchmark Guide

This document explains exactly how the 18-step benchmarking suite works, where your results will be saved, and the step-by-step process you need to follow.

## How it Works
We are using `metrics_capture.py` combined with Wireshark to measure the network performance between your testing laptop and the receiver device.
- **The Application Time:** Starts exactly when the Flutter Dart code enters the `_sendLoop` and ends exactly when the `_finish` method is called. 
- **The True Network Time:** The Dart code automatically writes its start/end timestamps to `app_timestamps.json`. The Python script reads this file and strictly crops the Wireshark packet capture to exactly match the application's perceived transfer window, perfectly aligning User Experience with network metrics!

## Where Results Save
When you run a test with iterations (e.g., `--test 1 --version original --run 1`), the script will automatically create a folder named `runs/original_test1_run1/`. 
Inside this folder, it saves:
- `capture.pcap` (The raw Wireshark packet capture)
- `cpu.json` (Your CPU efficiency logs)
- `metrics.json` (The detailed JSON dump of the network calculations)

It will also create a convenient Excel-friendly CSV file in the main folder named `summary_original_test1.csv`. **Because you are running the test 3 times, the script will automatically APPEND each run to the same CSV file**, making it incredibly easy to average out your results!

---

## Step-by-Step Execution Plan

### Step 1: Generate the Data
Open an administrator terminal in the `testing_scripts` folder and generate the 9 test folders:
```bash
python gen_testfiles.py
```

### Step 2: Verify IP Configuration
Open `experiment_generic.json` and ensure `my_ip` and `receiver_ips` perfectly match your testing machine and your receiver device.

### Step 3: Run the "Original" Baseline (3 Iterations per test)
1. On your sender device, checkout the original unmodified code branch.
2. Ensure the Dart timestamp patch is applied to `app/lib/provider/network/send_provider.dart`.
3. Compile and launch LocalSend. **Ensure Quick Save is ON on the receiver.**
4. Run Test 1. You will do this 3 times:
   ```bash
   python metrics_capture.py go experiment_generic.json --test 1 --version original --run 1
   python metrics_capture.py go experiment_generic.json --test 1 --version original --run 2
   python metrics_capture.py go experiment_generic.json --test 1 --version original --run 3
   ```
5. For each run:
   - Select the correct folder (e.g., `data/1`) in the LocalSend UI.
   - **Press [Enter] in the Python script**, then immediately **click Send in the LocalSend UI**.
   - When the receiver finishes saving, **type 'A' and press [Enter] in the Python script**. (The Python script automatically aligns the PCAP to the Dart timestamps, so your reaction time here doesn't matter!)
   - Delete the received folder from your receiver's device.
6. Repeat this process for `--test 2` all the way to `--test 9`.

### Step 4: Run the "Batching" Benchmark
1. Checkout the `binary_batching` branch.
2. Apply the Dart timestamp patch using `git stash pop` or manual merge.
3. Recompile and launch LocalSend.
4. Run Test 1 through 9 again (3 iterations each), changing the version flag:
   ```bash
   python metrics_capture.py go experiment_generic.json --test 1 --version batching --run 1
   ```

---

## Do I need to run anything after?
**NO!** 
Because we added the automated analysis logic to `cmd_go`, the instant you type 'A' to stop the Wireshark capture, `metrics_capture.py` automatically parses the `.pcap` data using the exact Dart timestamps and generates the final statistics. 

Just open `summary_original_test1.csv` and `summary_batching_test1.csv` to compare your 3 runs!

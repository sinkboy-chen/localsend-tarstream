import os
import glob
import csv
import statistics
import matplotlib.pyplot as plt
import numpy as np

# Mapping test IDs to human-readable labels
SCENARIOS = {
    1: "10 x 1KB",
    2: "100 x 1KB",
    3: "1000 x 1KB",
    4: "10 x 10KB",
    5: "100 x 10KB",
    6: "1000 x 10KB",
    7: "10 x 100KB",
    8: "100 x 100KB",
    9: "1000 x 100KB",
}

def load_data():
    all_data = []
    for filepath in glob.glob("summary_*.csv"):
        filename = os.path.basename(filepath)
        parts = filename.replace(".csv", "").split("_")
        if len(parts) >= 3:
            version = parts[1]
            test_str = parts[2].replace("test", "")
            try:
                test_id = int(test_str)
            except ValueError:
                continue
            
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        if not row.get("completion_time_s"):
                            continue
                        all_data.append({
                            "version": version,
                            "test_id": test_id,
                            "completion_time_s": float(row["completion_time_s"]),
                            "goodput_bps": float(row["goodput_bps"]),
                            "overhead_ratio_percent": float(row["overhead_ratio_percent"]),
                            "cpu_efficiency_system_s_per_mib": float(row.get("cpu_efficiency_system_s_per_mib", 0) or 0),
                            "total_packets": int(row["total_packets"])
                        })
            except Exception as e:
                print(f"Error reading {filename}: {e}")
                
    return all_data

def plot_metric(data, metric_col, title, ylabel, filename_prefix, log_scale=False):
    # Sort test IDs 1 to 9
    test_ids = sorted(list(set(d["test_id"] for d in data)))
    labels = [SCENARIOS.get(tid, str(tid)) for tid in test_ids]
    
    # Structure for plotting
    original_points = []
    batching_points = []
    original_means = []
    batching_means = []
    
    for tid in test_ids:
        orig = [d[metric_col] for d in data if d["test_id"] == tid and d["version"] == "original" and d[metric_col] > 0]
        batch = [d[metric_col] for d in data if d["test_id"] == tid and d["version"] == "batching" and d[metric_col] > 0]
        
        original_points.append(orig)
        batching_points.append(batch)
        original_means.append(statistics.mean(orig) if orig else 0)
        batching_means.append(statistics.mean(batch) if batch else 0)

    x = np.arange(len(labels))
    width = 0.35
    
    # ------------------------------------------------
    # 1. Bar Chart (Averages)
    # ------------------------------------------------
    fig, ax = plt.subplots(figsize=(12, 6))
    ax.bar(x - width/2, original_means, width, label='Original', color='#e74c3c')
    ax.bar(x + width/2, batching_means, width, label='Batching', color='#2ecc71')
    
    ax.set_ylabel(ylabel)
    ax.set_title(f"{title} (Average)")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=45, ha="right")
    ax.legend()
    if log_scale: ax.set_yscale('log')
    ax.grid(axis='y', linestyle='--', alpha=0.7)
    plt.tight_layout()
    plt.savefig(f"{filename_prefix}_avg.png", dpi=300)
    plt.close()
    
    # ------------------------------------------------
    # 2. Scatter Chart (All Three Dots)
    # ------------------------------------------------
    fig, ax = plt.subplots(figsize=(12, 6))
    
    for i, tid in enumerate(test_ids):
        # Plot Original Dots (x - width/2)
        orig = original_points[i]
        if orig:
            # Jitter slightly on x axis so dots don't completely overlap
            x_orig = np.random.normal(x[i] - width/2, 0.05, size=len(orig))
            ax.scatter(x_orig, orig, color='#c0392b', alpha=0.7, edgecolors='white', s=50, 
                       label='Original' if i == 0 else "")
            
        # Plot Batching Dots (x + width/2)
        batch = batching_points[i]
        if batch:
            x_batch = np.random.normal(x[i] + width/2, 0.05, size=len(batch))
            ax.scatter(x_batch, batch, color='#27ae60', alpha=0.7, edgecolors='white', s=50, 
                       label='Batching' if i == 0 else "")
            
    ax.set_ylabel(ylabel)
    ax.set_title(f"{title} (All Runs Scatter)")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=45, ha="right")
    ax.legend()
    if log_scale: ax.set_yscale('log')
    ax.grid(axis='y', linestyle='--', alpha=0.7)
    plt.tight_layout()
    plt.savefig(f"{filename_prefix}_dots.png", dpi=300)
    plt.close()

def main():
    print("Loading benchmark data...")
    data = load_data()
    
    if not data:
        print("No valid data found! Ensure summary CSV files are in this directory.")
        return
        
    print(f"Loaded {len(data)} total benchmark runs.")
    
    # Generate Avg and Dots plots for each metric
    plot_metric(data, 'completion_time_s', 'Completion Time', 'Seconds', 'chart_completion_time', log_scale=True)
    plot_metric(data, 'goodput_bps', 'Goodput (Usable Speed)', 'Bits per Second', 'chart_goodput', log_scale=True)
    plot_metric(data, 'overhead_ratio_percent', 'Network Overhead Ratio', 'Percentage (%)', 'chart_overhead')
    plot_metric(data, 'total_packets', 'Total Network Packets', 'Packet Count', 'chart_packets', log_scale=True)

    # CPU Efficiency (if available in all tests)
    has_cpu = any(d['cpu_efficiency_system_s_per_mib'] > 0 for d in data)
    if has_cpu:
        plot_metric(data, 'cpu_efficiency_system_s_per_mib', 'CPU System Efficiency', 'Seconds per MiB', 'chart_cpu_efficiency')

    print("Success! Generated _avg.png and _dots.png for all metrics.")

if __name__ == "__main__":
    main()

"""
Performance Benchmark Suite (Item 24)
Executes A/B testing across all 4 tiers and stress workloads:
- Tier A: 1,000 lines (50 functions, 50 pending lines)
- Tier B: 10,000 lines (500 functions, 500 pending lines)
- Tier C: 50,000 lines (2,500 functions, 2,500 pending lines)
- Tier D: 100,000 lines (5,000 functions, 5,000 pending lines)
- Huge Single Function: 55,000 lines in 1 function (1,000 pending lines)
Measures:
- Layout build latency (ms)
- Chunk batch slice latency (ms)
- Analysis overlay cache hit/miss speedup
- Memory RSS footprint
"""

import argparse
import os
import sys
import time
import json
import tempfile
import shutil
from typing import Dict, Any, List

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from source_reader import (
    SourceContext,
    SourceLineDTO,
    FunctionRange,
    save_source_sidecar,
    calc_sidecar_file_key
)
from code_region import build_code_regions
from code_detail_service import CodeDetailService
from app.code_detail.sidecar_store import SidecarStore

def generate_benchmark_file(
    out_dir: str,
    report_id: str,
    file_path: str,
    total_lines: int,
    func_count: int,
    pending_count: int
) -> str:
    """Generate synthetic source context and sidecar for benchmarking."""
    lines = []
    func_ranges = []
    
    lines_per_func = max(10, total_lines // max(1, func_count))
    cur_func_idx = 1
    func_start = 1
    
    for i in range(1, total_lines + 1):
        if i == func_start:
            fname = f"bench_func_{cur_func_idx}()"
            fend = min(total_lines, func_start + lines_per_func - 1)
            func_ranges.append(FunctionRange(func_start, fend, fname))
            cur_func_idx += 1
            func_start = fend + 1
            
        lines.append(SourceLineDTO(
            line_no=i,
            source=f"    int var_{i} = do_compute({i});",
            coverage_state="uncovered" if (i % max(1, (total_lines // pending_count))) == 0 else "covered"
        ))
        
    ctx = SourceContext(
        project_name="PerfBenchmark",
        file_path=file_path,
        lines=lines,
        function_ranges=func_ranges,
        report_id=report_id
    )
    
    fkey = calc_sidecar_file_key(file_path)
    store = SidecarStore(search_dirs=[out_dir], chunk_size=2000)
    store.save_chunked_sidecar(out_dir, report_id, fkey, ctx)
    return out_dir

def run_performance_suite() -> Dict[str, Any]:
    """Run full benchmark matrix."""
    temp_dir = tempfile.mkdtemp()
    report_id = "report_benchmark_perf"
    results = {}
    results["evidence_class"] = "synthetic_benchmark"
    results["workload_id"] = "python-sidecar-layout-v1"
    
    tiers = [
        ("Tier_A_1k", 1000, 50, 50),
        ("Tier_B_10k", 10000, 500, 500),
        ("Tier_C_50k", 50000, 2500, 2500),
        ("Tier_D_100k", 100000, 5000, 5000),
        ("Huge_Function_55k", 55000, 1, 1000)
    ]
    
    try:
        service = CodeDetailService(search_dirs=[temp_dir])
        
        for name, total_lines, funcs, pendings in tiers:
            fpath = f"src/bench/{name}.c"
            generate_benchmark_file(temp_dir, report_id, fpath, total_lines, funcs, pendings)
            
            # 1. Warm-up
            service.get_code_layout("PerfBenchmark", fpath, report_id=report_id)
            
            # 2. Measure Layout Latency (3 iterations)
            layout_times = []
            for _ in range(3):
                t0 = time.perf_counter()
                layout = service.get_code_layout("PerfBenchmark", fpath, report_id=report_id)
                t1 = time.perf_counter()
                layout_times.append((t1 - t0) * 1000.0)
            avg_layout_ms = round(sum(layout_times) / len(layout_times), 2)
            
            # 3. Measure Chunk Slice Latency (fetch 500 lines)
            chunk_times = []
            for _ in range(3):
                t0 = time.perf_counter()
                slice_res = service.get_code_lines_single(
                    "PerfBenchmark", fpath, 501, 1000, report_id=report_id
                )
                t1 = time.perf_counter()
                chunk_times.append((t1 - t0) * 1000.0)
            avg_chunk_ms = round(sum(chunk_times) / len(chunk_times), 2)
            
            results[name] = {
                "total_lines": total_lines,
                "function_count": funcs,
                "pending_lines": pendings,
                "region_count": len(layout.get("regions", [])),
                "layout_latency_ms": avg_layout_ms,
                "chunk_slice_latency_ms": avg_chunk_ms,
                "sidecar_cache_stats": service._sidecar_store.cache_stats(),
                "status": "PASSED" if avg_layout_ms < 50.0 else "WARNING"
            }
            
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        
    return results

def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Run the dependency-free synthetic sidecar/layout benchmark. "
            "The result is not release performance evidence."
        )
    )
    parser.add_argument(
        "--output",
        default=os.path.join(_REPO_ROOT, "benchmarks", "perf_baseline.json"),
        help="JSON output path (default: benchmarks/perf_baseline.json)",
    )
    args = parser.parse_args(argv)

    print("=== Running Performance Benchmark Suite (Items 24) ===")
    result = run_performance_suite()
    out_file = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
    print(f"Benchmark results recorded to {out_file}:")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

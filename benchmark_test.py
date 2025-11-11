#!/usr/bin/env python3
"""
Benchmark test suite for ArrowRedis.

Generates and tests Arrow files of various sizes:
- 50 MB
- 250 MB
- 512 MB
- 1 GB

Tests the full pipeline: generate -> split -> read

Usage:
    # Run all benchmarks
    python benchmark_test.py --redis-url redis://localhost:6379/0
    
    # Run specific size
    python benchmark_test.py --redis-url redis://localhost:6379/0 --size 250mb
    
    # Skip cleanup (keep files and Redis keys)
    python benchmark_test.py --redis-url redis://localhost:6379/0 --no-cleanup
    
    # Custom output directory
    python benchmark_test.py --redis-url redis://localhost:6379/0 --output-dir ./benchmarks
"""

from __future__ import annotations
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Any
from dataclasses import dataclass, asdict

# Import from bench_arrow
from bench_arrow import (
    generate_one_ipc_file,
    split_ipc_to_redis,
    read_from_redis,
    open_redis,
)


@dataclass
class BenchmarkConfig:
    """Configuration for a benchmark test."""
    name: str
    target_size_mb: float
    partitions: int
    batches: int
    rows: int
    compression: str = "zstd"
    dict_strings: bool = True
    string_cardinality: int = 5000
    
    @property
    def estimated_size_mb(self) -> float:
        """Rough estimate of file size."""
        # Approximate: 200 bytes per row with compression
        total_rows = self.partitions * self.batches * self.rows
        return (total_rows * 200) / (1024 ** 2)


@dataclass
class BenchmarkResult:
    """Results from a benchmark run."""
    config: BenchmarkConfig
    
    # Generation metrics
    gen_time_sec: float
    gen_file_size_mb: float
    gen_throughput_mb_sec: float
    gen_throughput_rows_sec: float
    
    # Split metrics
    split_time_sec: float
    split_chunks: int
    split_throughput_mb_sec: float
    
    # Read metrics
    read_time_sec: float
    read_fetch_time_sec: float
    read_parse_time_sec: float
    read_rows: int
    read_throughput_mb_sec: float
    
    # Overall
    total_time_sec: float
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result = asdict(self)
        result['config'] = asdict(self.config)
        return result
    
    def print_summary(self):
        """Print human-readable summary."""
        print(f"\n{'='*80}")
        print(f"Benchmark: {self.config.name}")
        print(f"{'='*80}")
        print(f"Configuration:")
        print(f"  Partitions: {self.config.partitions}")
        print(f"  Batches/partition: {self.config.batches}")
        print(f"  Rows/batch: {self.config.rows:,}")
        print(f"  Total rows: {self.config.partitions * self.config.batches * self.config.rows:,}")
        print(f"  Compression: {self.config.compression}")
        print(f"  Dictionary strings: {self.config.dict_strings}")
        print(f"\nGeneration:")
        print(f"  Time: {self.gen_time_sec:.2f}s")
        print(f"  File size: {self.gen_file_size_mb:.2f} MB")
        print(f"  Throughput: {self.gen_throughput_mb_sec:.2f} MB/s, {self.gen_throughput_rows_sec:,.0f} rows/s")
        print(f"\nRedis Split:")
        print(f"  Time: {self.split_time_sec:.2f}s")
        print(f"  Chunks: {self.split_chunks}")
        print(f"  Throughput: {self.split_throughput_mb_sec:.2f} MB/s")
        print(f"\nRedis Read:")
        print(f"  Time: {self.read_time_sec:.2f}s")
        print(f"  Fetch time: {self.read_fetch_time_sec:.2f}s")
        print(f"  Parse time: {self.read_parse_time_sec:.2f}s")
        print(f"  Rows read: {self.read_rows:,}")
        print(f"  Throughput: {self.read_throughput_mb_sec:.2f} MB/s")
        print(f"\nTotal Time: {self.total_time_sec:.2f}s")
        print(f"{'='*80}\n")


# Predefined benchmark configurations
BENCHMARK_CONFIGS = {
    "50mb": BenchmarkConfig(
        name="50MB",
        target_size_mb=50,
        partitions=4,
        batches=8,
        rows=50_000,
    ),
    "250mb": BenchmarkConfig(
        name="250MB",
        target_size_mb=250,
        partitions=8,
        batches=16,
        rows=50_000,
    ),
    "512mb": BenchmarkConfig(
        name="512MB",
        target_size_mb=512,
        partitions=16,
        batches=16,
        rows=50_000,
    ),
    "1gb": BenchmarkConfig(
        name="1GB",
        target_size_mb=1024,
        partitions=32,
        batches=16,
        rows=50_000,
    ),
}


async def run_benchmark(
    config: BenchmarkConfig,
    redis_url: str,
    output_dir: Path,
    cluster: bool = False,
    max_inflight: int = 256,
    pipeline: int = 64,
    concurrency: int = 256,
) -> BenchmarkResult:
    """
    Run a complete benchmark: generate -> split -> read.
    """
    print(f"\n🚀 Starting benchmark: {config.name}")
    print(f"   Target size: ~{config.target_size_mb} MB")
    print(f"   Estimated size: ~{config.estimated_size_mb:.1f} MB")
    
    output_dir.mkdir(parents=True, exist_ok=True)
    arrow_file = output_dir / f"benchmark_{config.name.lower()}.arrow"
    prefix = f"bench:{config.name.lower()}"
    
    total_start = time.perf_counter()
    
    # 1. Generate Arrow file
    print(f"\n📝 Generating Arrow file...")
    gen_start = time.perf_counter()
    generate_one_ipc_file(
        out=arrow_file,
        partitions=config.partitions,
        batches=config.batches,
        rows=config.rows,
        compression=config.compression,
        dict_strings=config.dict_strings,
        string_card=config.string_cardinality,
        seed=42,
    )
    gen_time = time.perf_counter() - gen_start
    
    file_size_bytes = arrow_file.stat().st_size
    file_size_mb = file_size_bytes / (1024 ** 2)
    total_rows = config.partitions * config.batches * config.rows
    gen_throughput_mb = file_size_mb / gen_time
    gen_throughput_rows = total_rows / gen_time
    
    print(f"   ✅ Generated {file_size_mb:.2f} MB in {gen_time:.2f}s")
    print(f"   ⚡ Throughput: {gen_throughput_mb:.2f} MB/s, {gen_throughput_rows:,.0f} rows/s")
    
    # 2. Split to Redis
    print(f"\n📤 Splitting to Redis...")
    split_start = time.perf_counter()
    chunks = await split_ipc_to_redis(
        ipc_path=arrow_file,
        redis_url=redis_url,
        prefix=prefix,
        batches_per_partition=config.batches,
        compression=config.compression,
        cluster=cluster,
        max_inflight=max_inflight,
    )
    split_time = time.perf_counter() - split_start
    split_throughput_mb = file_size_mb / split_time
    
    print(f"   ✅ Uploaded {chunks} chunks in {split_time:.2f}s")
    print(f"   ⚡ Throughput: {split_throughput_mb:.2f} MB/s")
    
    # 3. Read from Redis (all partitions)
    print(f"\n📥 Reading from Redis...")
    partitions_to_read = list(range(config.partitions))
    
    read_start = time.perf_counter()
    
    # Monkey-patch to capture timing details
    import bench_arrow
    original_read = bench_arrow.read_from_redis
    
    fetch_time = 0.0
    parse_time = 0.0
    rows_read = 0
    
    async def instrumented_read(*args, **kwargs):
        nonlocal fetch_time, parse_time, rows_read
        
        # Call original but capture the printed metrics
        import io
        from contextlib import redirect_stdout
        
        f = io.StringIO()
        with redirect_stdout(f):
            table = await original_read(*args, **kwargs)
        
        output = f.getvalue()
        # Parse output: "Fetched X/Y present chunks in Xs; parsed in Ys; rows=Z"
        import re
        match = re.search(r'in ([\d.]+)s; parsed in ([\d.]+)s; rows=([\d,]+)', output)
        if match:
            fetch_time = float(match.group(1))
            parse_time = float(match.group(2))
            rows_read = int(match.group(3).replace(',', ''))
        
        return table
    
    bench_arrow.read_from_redis = instrumented_read
    
    try:
        table = await read_from_redis(
            redis_url=redis_url,
            prefix=prefix,
            partitions=partitions_to_read,
            batches_per_part=config.batches,
            pipeline=pipeline,
            concurrency=concurrency,
            cluster=cluster,
        )
    finally:
        bench_arrow.read_from_redis = original_read
    
    read_time = time.perf_counter() - read_start
    read_throughput_mb = file_size_mb / read_time
    
    print(f"   ✅ Read {rows_read:,} rows in {read_time:.2f}s")
    print(f"   ⚡ Throughput: {read_throughput_mb:.2f} MB/s")
    
    total_time = time.perf_counter() - total_start
    
    # Create result
    result = BenchmarkResult(
        config=config,
        gen_time_sec=gen_time,
        gen_file_size_mb=file_size_mb,
        gen_throughput_mb_sec=gen_throughput_mb,
        gen_throughput_rows_sec=gen_throughput_rows,
        split_time_sec=split_time,
        split_chunks=chunks,
        split_throughput_mb_sec=split_throughput_mb,
        read_time_sec=read_time,
        read_fetch_time_sec=fetch_time,
        read_parse_time_sec=parse_time,
        read_rows=rows_read,
        read_throughput_mb_sec=read_throughput_mb,
        total_time_sec=total_time,
    )
    
    return result


async def cleanup_redis(redis_url: str, prefix: str, cluster: bool):
    """Clean up Redis keys for a benchmark."""
    print(f"🧹 Cleaning up Redis keys with prefix '{prefix}:*'...")
    r = await open_redis(redis_url, cluster)
    
    # Scan and delete keys
    cursor = 0
    deleted = 0
    while True:
        cursor, keys = await r.scan(cursor, match=f"{prefix}:*", count=1000)
        if keys:
            await r.delete(*keys)
            deleted += len(keys)
        if cursor == 0:
            break
    
    await r.close()
    print(f"   ✅ Deleted {deleted} keys")


async def main_async():
    parser = argparse.ArgumentParser(description="Benchmark ArrowRedis with various file sizes")
    parser.add_argument("--redis-url", required=True, help="Redis connection URL")
    parser.add_argument("--size", choices=list(BENCHMARK_CONFIGS.keys()) + ["all"], 
                       default="all", help="Benchmark size to run (default: all)")
    parser.add_argument("--output-dir", type=Path, default=Path("./benchmark_data"),
                       help="Directory for benchmark files (default: ./benchmark_data)")
    parser.add_argument("--cluster", action="store_true", help="Use Redis Cluster mode")
    parser.add_argument("--no-cleanup", action="store_true", 
                       help="Don't clean up files and Redis keys after benchmark")
    parser.add_argument("--max-inflight", type=int, default=256,
                       help="Max concurrent Redis operations (default: 256)")
    parser.add_argument("--pipeline", type=int, default=64,
                       help="Keys per MGET (default: 64)")
    parser.add_argument("--concurrency", type=int, default=256,
                       help="Concurrent MGET operations (default: 256)")
    parser.add_argument("--output-json", type=Path, default=None,
                       help="Save results to JSON file")
    
    args = parser.parse_args()
    
    # Determine which benchmarks to run
    if args.size == "all":
        configs = list(BENCHMARK_CONFIGS.values())
    else:
        configs = [BENCHMARK_CONFIGS[args.size]]
    
    print(f"\n{'='*80}")
    print(f"ArrowRedis Benchmark Suite")
    print(f"{'='*80}")
    print(f"Redis URL: {args.redis_url}")
    print(f"Output directory: {args.output_dir}")
    print(f"Benchmarks: {', '.join(c.name for c in configs)}")
    print(f"{'='*80}\n")
    
    results: List[BenchmarkResult] = []
    
    for config in configs:
        try:
            result = await run_benchmark(
                config=config,
                redis_url=args.redis_url,
                output_dir=args.output_dir,
                cluster=args.cluster,
                max_inflight=args.max_inflight,
                pipeline=args.pipeline,
                concurrency=args.concurrency,
            )
            results.append(result)
            result.print_summary()
            
            # Cleanup Redis if requested
            if not args.no_cleanup:
                await cleanup_redis(
                    redis_url=args.redis_url,
                    prefix=f"bench:{config.name.lower()}",
                    cluster=args.cluster,
                )
        
        except Exception as e:
            print(f"❌ Benchmark {config.name} failed: {e}")
            import traceback
            traceback.print_exc()
    
    # Cleanup files if requested
    if not args.no_cleanup:
        print(f"\n🧹 Cleaning up benchmark files...")
        for config in configs:
            arrow_file = args.output_dir / f"benchmark_{config.name.lower()}.arrow"
            if arrow_file.exists():
                arrow_file.unlink()
                print(f"   ✅ Deleted {arrow_file}")
    
    # Save results to JSON if requested
    if args.output_json and results:
        with open(args.output_json, 'w') as f:
            json.dump([r.to_dict() for r in results], f, indent=2)
        print(f"\n💾 Results saved to {args.output_json}")
    
    # Print summary table
    if results:
        print(f"\n{'='*80}")
        print(f"Summary")
        print(f"{'='*80}")
        print(f"{'Benchmark':<12} {'Size (MB)':<12} {'Gen (s)':<10} {'Split (s)':<10} {'Read (s)':<10} {'Total (s)':<10}")
        print(f"{'-'*80}")
        for r in results:
            print(f"{r.config.name:<12} {r.gen_file_size_mb:<12.2f} {r.gen_time_sec:<10.2f} "
                  f"{r.split_time_sec:<10.2f} {r.read_time_sec:<10.2f} {r.total_time_sec:<10.2f}")
        print(f"{'='*80}\n")


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()


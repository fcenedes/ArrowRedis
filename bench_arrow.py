#!/usr/bin/env python3
"""
Async Arrow (IPC) one-file generator + Redis splitter + parallel async readers.

Usage
-----
# 1) Generate ONE large IPC file
python bench_arrow.py \
  gen --out ./dataset.arrow --partitions 32 --batches 16 --rows 100000 \
  --compression zstd --dict-strings --seed 42

# 2) Split that file into Redis as per-(partition,batch) IPC streams (same compression)
python bench_arrow.py \
  split --inp ./dataset.arrow --redis-url redis://localhost:6379/0 \
  --prefix demo:v1 --compression zstd --cluster off --max-inflight 256

# 3) Read back selected partitions in parallel from Redis (async MGET + parse)
python bench_arrow.py \
  read --redis-url redis://localhost:6379/0 --prefix demo:v1 \
  --partitions 0,1,2 --batches 16 --pipeline 64 --concurrency 256 --cluster off
"""

from __future__ import annotations
import argparse, asyncio, io, logging, sys, time
from pathlib import Path
from typing import List, Dict, Optional, TypeVar, Callable
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import cpu_count

import numpy as np
import pyarrow as pa
import pyarrow.ipc as ipc
from tqdm import tqdm

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ---- uvloop (optional) ----
try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except Exception:
    pass  # ok if uvloop not installed

# ---- redis async clients ----
# redis-py 5.x
from redis.asyncio import Redis
try:
    from redis.asyncio.cluster import RedisCluster  # available if redis>=4.3
except Exception:
    RedisCluster = None

# ---- S3 support ----
try:
    import boto3
    from pyarrow import fs as pafs
    S3_AVAILABLE = True
except ImportError:
    S3_AVAILABLE = False


# ----------------------------
# Retry utility
# ----------------------------
T = TypeVar('T')

async def retry_with_backoff(
    func: Callable[..., T],
    *args,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    **kwargs
) -> T:
    """Retry async function with exponential backoff."""
    for attempt in range(max_retries):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            if attempt == max_retries - 1:
                raise

            delay = min(base_delay * (2 ** attempt), max_delay)
            logger.warning(f"Attempt {attempt + 1}/{max_retries} failed: {e}. Retrying in {delay:.1f}s...")
            await asyncio.sleep(delay)

    raise RuntimeError("Should not reach here")


# ----------------------------
# Data generation: ONE big IPC
# ----------------------------
def _rand_vocab(card: int, rng: np.random.Generator) -> list[str]:
    v = np.array(list("aeiou")); c = np.array(list("bcdfghjklmnpqrstvwxyz"))
    out = []
    for _ in range(card):
        syls = rng.integers(2, 5)
        out.append("".join("".join([rng.choice(c), rng.choice(v)]) for _ in range(syls)))
    return out

def _make_batch(rows: int, partition_id: int, global_batch_id: int,
                dict_strings: bool, vocab: list[str], rng: np.random.Generator) -> pa.RecordBatch:
    id64 = pa.array(np.arange(global_batch_id*rows, global_batch_id*rows + rows, dtype=np.int64))
    part = pa.array(np.full(rows, partition_id, dtype=np.int32))
    i32  = pa.array(rng.integers(-1_000_000, 1_000_000, size=rows, dtype=np.int32))
    f64  = pa.array(rng.standard_normal(rows))
    f32  = pa.array(rng.standard_normal(rows).astype(np.float32))
    b    = pa.array(rng.random(rows) < 0.5)

    base_ns = np.int64(1_700_000_000_000_000_000)
    span_ns = np.int64(30 * 24 * 3600 * 1_000_000_000)
    ts   = pa.array(base_ns + rng.integers(0, span_ns, size=rows, dtype=np.int64), type=pa.timestamp("ns"))

    # Create decimal values properly
    from decimal import Decimal
    dec_vals = [Decimal(str(x / 10000.0)) for x in rng.integers(-10**10, 10**10, size=rows, dtype=np.int64)]
    dec  = pa.array(dec_vals, type=pa.decimal128(18, 4))

    string_card = len(vocab)
    idx = rng.integers(0, string_card, size=rows, dtype=np.int32)
    if dict_strings:
        s = pa.DictionaryArray.from_arrays(pa.array(idx, pa.int32()), pa.array(vocab, pa.string()))
    else:
        s = pa.array([vocab[i] for i in idx], type=pa.string())

    counts = rng.integers(0, 6, size=rows)
    vals, offs = [], [0]
    for c in counts:
        if c: vals.extend(rng.integers(0, 1000, size=int(c), dtype=np.int32).tolist())
        offs.append(offs[-1] + int(c))
    list_vals = pa.array(vals, type=pa.int32())
    list_offs = pa.array(offs, type=pa.int32())
    lst = pa.ListArray.from_arrays(list_offs, list_vals)

    cols = [
        ("id64", id64), ("partition", part), ("int32_c", i32), ("float64_c", f64),
        ("float32_c", f32), ("bool_c", b), ("ts_ns", ts), ("dec_18_4", dec),
        ("str_c", s), ("list_ints", lst),
    ]
    return pa.record_batch([c for _, c in cols], names=[n for n, _ in cols])

def _generate_batch_worker(args):
    """
    Worker function for parallel batch generation.
    Returns serialized RecordBatch as bytes.
    """
    rows, partition_id, global_batch_id, dict_strings, vocab, seed_offset = args
    rng = np.random.default_rng(seed_offset)
    batch = _make_batch(rows, partition_id, global_batch_id, dict_strings, vocab, rng)

    # Serialize to bytes for IPC transfer
    sink = pa.BufferOutputStream()
    writer = pa.ipc.RecordBatchStreamWriter(sink, batch.schema)
    writer.write_batch(batch)
    writer.close()
    return sink.getvalue().to_pybytes()

def generate_one_ipc_file(out: Path, partitions: int, batches: int, rows: int,
                          compression: str, dict_strings: bool, string_card: int, seed: int,
                          parallel: bool = True, max_workers: Optional[int] = None) -> Dict[str, float]:
    """
    Generate Arrow IPC file with optional parallel batch generation.

    Args:
        parallel: If True, use ProcessPoolExecutor for parallel batch generation
        max_workers: Number of worker processes (default: cpu_count())
    """
    t_start = time.perf_counter()

    rng = np.random.default_rng(seed)

    # Generate vocabulary ONCE and reuse across all batches
    t_vocab = time.perf_counter()
    vocab = _rand_vocab(string_card, rng)
    t_vocab = time.perf_counter() - t_vocab

    total_batches = partitions * batches
    first = _make_batch(rows, partition_id=0, global_batch_id=0,
                        dict_strings=dict_strings, vocab=vocab, rng=rng)
    opts = ipc.IpcWriteOptions(compression=None if compression == "uncompressed" else compression)

    # Use parallel only if beneficial (enough batches to offset process overhead)
    # Heuristic: parallel is beneficial when total_batches >= 64 or rows >= 50000
    use_parallel = parallel and (total_batches >= 64 or (total_batches >= 16 and rows >= 50000))

    if use_parallel:
        # Parallel generation
        if max_workers is None:
            max_workers = min(cpu_count(), total_batches)

        logger.info(f"Using parallel generation with {max_workers} workers ({total_batches} batches)")

        # Prepare batch arguments
        batch_args = []
        for p in range(partitions):
            for b in range(batches):
                if p == 0 and b == 0:
                    continue  # Skip first batch (already generated)
                global_batch_id = p * batches + b
                seed_offset = seed + global_batch_id
                batch_args.append((rows, p, global_batch_id, dict_strings, vocab, seed_offset))

        with pa.OSFile(str(out), "wb") as sink, \
             ipc.RecordBatchFileWriter(sink, first.schema, options=opts) as w, \
             ProcessPoolExecutor(max_workers=max_workers) as executor, \
             tqdm(total=total_batches, desc="Generating batches", unit="batch", disable=None) as pbar:

            # Write first batch
            w.write_batch(first)
            pbar.update(1)

            # Submit all batch generation tasks
            futures = [executor.submit(_generate_batch_worker, args) for args in batch_args]

            # Write batches as they complete
            for future in futures:
                batch_bytes = future.result()
                # Deserialize batch
                reader = pa.ipc.RecordBatchStreamReader(batch_bytes)
                batch = reader.read_next_batch()
                w.write_batch(batch)
                pbar.update(1)
    else:
        # Sequential generation (original code)
        if parallel:
            logger.info(f"Using sequential generation ({total_batches} batches - parallel overhead not beneficial)")
        else:
            logger.info("Using sequential generation")

        with pa.OSFile(str(out), "wb") as sink, \
             ipc.RecordBatchFileWriter(sink, first.schema, options=opts) as w, \
             tqdm(total=total_batches, desc="Generating batches", unit="batch", disable=None) as pbar:

            w.write_batch(first)
            pbar.update(1)

            for p in range(partitions):
                start_b = 0 if p != 0 else 1
                for b in range(start_b, batches):
                    rb = _make_batch(rows, partition_id=p, global_batch_id=(p*batches+b),
                                     dict_strings=dict_strings, vocab=vocab, rng=rng)
                    w.write_batch(rb)
                    pbar.update(1)

    t_total = time.perf_counter() - t_start
    file_size = out.stat().st_size
    file_size_mb = file_size / (1024**2)
    total_rows = partitions * batches * rows

    metrics = {
        'total_time': t_total,
        'vocab_time': t_vocab,
        'file_size_bytes': file_size,
        'file_size_mb': file_size_mb,
        'total_rows': total_rows,
        'throughput_rows_per_sec': total_rows / t_total,
        'throughput_mb_per_sec': file_size_mb / t_total,
    }

    logger.info(f"Generated {file_size_mb:.2f} MB in {t_total:.2f}s")
    logger.info(f"Throughput: {metrics['throughput_mb_per_sec']:.2f} MB/s, {metrics['throughput_rows_per_sec']:,.0f} rows/s")

    return metrics


# ---------------------------------
# Redis connection helpers (async)
# ---------------------------------
async def open_redis(url: str, cluster: bool,
                     max_connections: int = 50,
                     socket_keepalive: bool = True,
                     socket_connect_timeout: float = 5.0,
                     retry_on_timeout: bool = True) -> Redis:
    """
    Open Redis connection with optimized pool settings.

    cluster=False  -> Redis (works with Redis Enterprise proxy like standalone)
    cluster=True   -> RedisCluster (keys must share hash slot for pipelines/MGET)
    """
    try:
        if cluster:
            if RedisCluster is None:
                raise RuntimeError("redis.asyncio.cluster.RedisCluster not available. Upgrade redis-py.")
            r = RedisCluster.from_url(
                url,
                decode_responses=False,
                readonly=True,
                max_connections=max_connections,
                socket_keepalive=socket_keepalive,
                socket_connect_timeout=socket_connect_timeout,
                retry_on_timeout=retry_on_timeout,
            )
        else:
            r = Redis.from_url(
                url,
                decode_responses=False,
                max_connections=max_connections,
                socket_keepalive=socket_keepalive,
                socket_connect_timeout=socket_connect_timeout,
                retry_on_timeout=retry_on_timeout,
            )
        # quick ping
        await r.ping()
        logger.info(f"Connected to Redis at {url}")
        return r
    except Exception as e:
        logger.error(f"Failed to connect to Redis at {url}: {e}")
        raise


# ---------------------------------
# Split ONE IPC into Redis (async)
# ---------------------------------
async def split_ipc_to_redis(ipc_path: Path, redis_url: str, prefix: str,
                             batches_per_partition: int, compression: str,
                             cluster: bool, max_inflight: int = 256,
                             batch_gather_size: int = 100) -> Dict[str, float]:
    t_start = time.perf_counter()

    try:
        r = await open_redis(redis_url, cluster)
    except Exception as e:
        logger.error(f"Failed to connect to Redis: {e}")
        raise

    try:
        reader = ipc.open_file(str(ipc_path))
    except Exception as e:
        logger.error(f"Failed to open Arrow file {ipc_path}: {e}")
        await r.close()
        raise

    file_size = ipc_path.stat().st_size
    file_size_mb = file_size / (1024**2)

    opts = ipc.IpcWriteOptions(compression=None if compression == "uncompressed" else compression)

    # Track per-partition batch index as we stream the file
    next_batch_idx: Dict[int, int] = {}

    sem = asyncio.Semaphore(max_inflight)
    put_tasks: List[asyncio.Task] = []

    async def put_one(k: str, payload: bytes):
        async with sem:
            await retry_with_backoff(r.set, k, payload, max_retries=3)

    total = 0
    try:
        with tqdm(total=reader.num_record_batches, desc="Uploading to Redis", unit="batch", disable=None) as pbar:
            for i in range(reader.num_record_batches):
                rb = reader.get_batch(i)
                p_idx = rb.schema.get_field_index("partition")
                if p_idx == -1:
                    # fallback if no column; map by linear order
                    part = i // batches_per_partition
                else:
                    part = int(rb.column(p_idx)[0].as_py())

                b_in_part = next_batch_idx.get(part, 0)
                next_batch_idx[part] = b_in_part + 1

                key = f"{prefix}:{{part={part:05d}}}:batch={b_in_part:05d}"

                sink = pa.BufferOutputStream()
                with ipc.new_stream(sink, rb.schema, options=opts) as w:
                    w.write_batch(rb)
                buf = sink.getvalue().to_pybytes()
                put_tasks.append(asyncio.create_task(put_one(key, buf)))
                total += 1
                pbar.update(1)

                # Gather in batches to avoid memory buildup
                if len(put_tasks) >= batch_gather_size:
                    await asyncio.gather(*put_tasks)
                    put_tasks.clear()

            # Final gather for remaining tasks
            if put_tasks:
                await asyncio.gather(*put_tasks)
    finally:
        await r.close()

    t_total = time.perf_counter() - t_start

    metrics = {
        'total_time': t_total,
        'chunks': total,
        'file_size_mb': file_size_mb,
        'throughput_mb_per_sec': file_size_mb / t_total,
    }

    logger.info(f"Uploaded {total} chunks ({file_size_mb:.2f} MB) in {t_total:.2f}s")
    logger.info(f"Throughput: {metrics['throughput_mb_per_sec']:.2f} MB/s")

    return metrics


# ---------------------------------
# Async parallel read from Redis
# ---------------------------------
async def discover_partitions(r: Redis, prefix: str) -> List[int]:
    """
    Discover all partition IDs from Redis by scanning keys with the given prefix.
    Returns sorted list of unique partition IDs.
    """
    import re
    partitions = set()
    pattern = f"{prefix}:{{part=*}}:batch=*"

    cursor = 0
    while True:
        cursor, keys = await r.scan(cursor, match=pattern, count=1000)
        for key in keys:
            # Extract partition ID from key like "prefix:{part=00000}:batch=00001"
            if isinstance(key, bytes):
                key = key.decode('utf-8')
            match = re.search(r'\{part=(\d+)\}', key)
            if match:
                partitions.add(int(match.group(1)))
        if cursor == 0:
            break

    return sorted(partitions)

async def mget_partition(r: Redis, keys: List[str]) -> List[Optional[bytes]]:
    """
    Single MGET for a set of keys that share the same hash slot (we enforce with {part=XXXXX}).
    For very large sets, consider chunking (we do in caller).
    """
    # Use a plain MGET; for cluster this works because keys share slot tag.
    return await retry_with_backoff(r.mget, keys, max_retries=3)

async def read_from_redis(redis_url: str, prefix: str, partitions: List[int] | None, batches_per_part: int,
                          pipeline: int, concurrency: int, cluster: bool) -> pa.Table:
    try:
        r = await open_redis(redis_url, cluster)
    except Exception as e:
        logger.error(f"Failed to connect to Redis: {e}")
        raise

    try:
        # If partitions is None, discover all partitions
        if partitions is None:
            logger.info(f"Discovering partitions from Redis with prefix '{prefix}'...")
            partitions = await discover_partitions(r, prefix)
            logger.info(f"Found {len(partitions)} partitions: {partitions}")
            if not partitions:
                logger.warning("No partitions found in Redis")
                return pa.table({})

        # Build keys per partition (hash-tagged)
        all_chunks: List[List[str]] = []
        for p in partitions:
            part_keys = [f"{prefix}:{{part={p:05d}}}:batch={b:05d}" for b in range(batches_per_part)]
            # chunk by pipeline size (MGET batch)
            all_chunks.extend([part_keys[i:i+pipeline] for i in range(0, len(part_keys), pipeline)])

        sem = asyncio.Semaphore(concurrency)
        results: List[Optional[bytes]] = []

        async def fetch_chunk(chunk: List[str]):
            async with sem:
                vals = await mget_partition(r, chunk)
                return vals

        t0 = time.perf_counter()
        vals_lists = await asyncio.gather(*[fetch_chunk(ch) for ch in all_chunks])
        t_fetch = time.perf_counter() - t0
        # flatten
        for vl in vals_lists:
            results.extend(vl)

        # Parse IPC streams concurrently (to thread pool; Arrow parsing is C++ & fast)
        def parse_one(buf: bytes) -> pa.Table:
            br = pa.BufferReader(buf)
            return ipc.open_stream(br).read_all()

        parse_inputs = [buf for buf in results if buf]
        t1 = time.perf_counter()
        tables = await asyncio.gather(*[asyncio.to_thread(parse_one, b) for b in parse_inputs])
        t_parse = time.perf_counter() - t1

        table = pa.concat_tables(tables, promote=True) if tables else pa.table({})
        logger.info(f"Fetched {len(parse_inputs)}/{len(results)} present chunks in {t_fetch:.2f}s; parsed in {t_parse:.2f}s; rows={table.num_rows:,}")
        return table
    finally:
        await r.close()


# ---------------------------------
# S3 Upload and Read
# ---------------------------------
def upload_to_s3(ipc_path: Path, s3_bucket: str, s3_key: str,
                 aws_access_key: Optional[str] = None,
                 aws_secret_key: Optional[str] = None,
                 aws_region: Optional[str] = None) -> Dict[str, float]:
    """
    Upload Arrow IPC file to S3.

    Args:
        ipc_path: Local Arrow IPC file path
        s3_bucket: S3 bucket name
        s3_key: S3 object key (path within bucket)
        aws_access_key: AWS access key (optional, uses default credentials if None)
        aws_secret_key: AWS secret key (optional)
        aws_region: AWS region (optional, defaults to us-east-1)

    Returns:
        Dictionary with metrics (file_size_mb, upload_time, throughput_mb_per_sec)
    """
    if not S3_AVAILABLE:
        raise ImportError("boto3 is required for S3 support. Install with: pip install boto3")

    t_start = time.perf_counter()

    # Create S3 client
    session_kwargs = {}
    if aws_access_key and aws_secret_key:
        session_kwargs['aws_access_key_id'] = aws_access_key
        session_kwargs['aws_secret_access_key'] = aws_secret_key
    if aws_region:
        session_kwargs['region_name'] = aws_region

    s3_client = boto3.client('s3', **session_kwargs)

    # Get file size
    file_size = ipc_path.stat().st_size
    file_size_mb = file_size / (1024**2)

    # Upload with progress bar
    logger.info(f"Uploading {file_size_mb:.2f} MB to s3://{s3_bucket}/{s3_key}")

    with tqdm(total=file_size, desc="Uploading to S3", unit="B", unit_scale=True, disable=None) as pbar:
        def callback(bytes_transferred):
            pbar.update(bytes_transferred)

        s3_client.upload_file(
            str(ipc_path),
            s3_bucket,
            s3_key,
            Callback=callback
        )

    upload_time = time.perf_counter() - t_start
    throughput_mb_per_sec = file_size_mb / upload_time if upload_time > 0 else 0

    logger.info(f"Uploaded {file_size_mb:.2f} MB in {upload_time:.2f}s ({throughput_mb_per_sec:.2f} MB/s)")

    return {
        'file_size_mb': file_size_mb,
        'upload_time': upload_time,
        'throughput_mb_per_sec': throughput_mb_per_sec,
    }


def read_from_s3(s3_bucket: str, s3_key: str, partitions: List[int],
                 aws_access_key: Optional[str] = None,
                 aws_secret_key: Optional[str] = None,
                 aws_region: Optional[str] = None) -> pa.Table:
    """
    Read Arrow IPC file from S3 using PyArrow's parallel reader.

    Args:
        s3_bucket: S3 bucket name
        s3_key: S3 object key (path within bucket)
        partitions: List of partition IDs to read (for filtering)
        aws_access_key: AWS access key (optional)
        aws_secret_key: AWS secret key (optional)
        aws_region: AWS region (optional)

    Returns:
        PyArrow Table with selected partitions
    """
    if not S3_AVAILABLE:
        raise ImportError("boto3 and pyarrow[s3] are required for S3 support")

    t_start = time.perf_counter()

    # Create S3 filesystem
    fs_kwargs = {}
    if aws_access_key and aws_secret_key:
        fs_kwargs['access_key'] = aws_access_key
        fs_kwargs['secret_key'] = aws_secret_key
    if aws_region:
        fs_kwargs['region'] = aws_region

    s3_fs = pafs.S3FileSystem(**fs_kwargs)

    # Read Arrow file from S3
    s3_path = f"{s3_bucket}/{s3_key}"
    logger.info(f"Reading from s3://{s3_path}")

    t_fetch_start = time.perf_counter()
    with s3_fs.open_input_file(s3_path) as f:
        reader = ipc.open_file(f)

        # Read all batches (PyArrow handles parallelism internally)
        batches = []
        with tqdm(total=reader.num_record_batches, desc="Reading from S3", unit="batch", disable=None) as pbar:
            for i in range(reader.num_record_batches):
                batch = reader.get_batch(i)

                # Filter by partition if needed
                if partitions:
                    p_idx = batch.schema.get_field_index("partition")
                    if p_idx != -1:
                        part_id = int(batch.column(p_idx)[0].as_py())
                        if part_id not in partitions:
                            pbar.update(1)
                            continue

                batches.append(batch)
                pbar.update(1)

    t_fetch = time.perf_counter() - t_fetch_start

    # Combine batches
    table = pa.Table.from_batches(batches) if batches else pa.table({})

    total_time = time.perf_counter() - t_start
    logger.info(f"Read {table.num_rows:,} rows from S3 in {total_time:.2f}s (fetch: {t_fetch:.2f}s)")

    return table


def read_from_local(ipc_path: Path, partitions: List[int]) -> pa.Table:
    """
    Read Arrow IPC file from local filesystem (baseline benchmark).

    Args:
        ipc_path: Local Arrow IPC file path
        partitions: List of partition IDs to read (for filtering)

    Returns:
        PyArrow Table with selected partitions
    """
    t_start = time.perf_counter()

    logger.info(f"Reading from local file: {ipc_path}")

    t_fetch_start = time.perf_counter()
    reader = ipc.open_file(str(ipc_path))

    # Read all batches
    batches = []
    with tqdm(total=reader.num_record_batches, desc="Reading from local", unit="batch", disable=None) as pbar:
        for i in range(reader.num_record_batches):
            batch = reader.get_batch(i)

            # Filter by partition if needed
            if partitions:
                p_idx = batch.schema.get_field_index("partition")
                if p_idx != -1:
                    part_id = int(batch.column(p_idx)[0].as_py())
                    if part_id not in partitions:
                        pbar.update(1)
                        continue

            batches.append(batch)
            pbar.update(1)

    t_fetch = time.perf_counter() - t_fetch_start

    # Combine batches
    table = pa.Table.from_batches(batches) if batches else pa.table({})

    total_time = time.perf_counter() - t_start
    logger.info(f"Read {table.num_rows:,} rows from local file in {total_time:.2f}s (fetch: {t_fetch:.2f}s)")

    return table


# ----------------------------
# CLI
# ----------------------------
def build_cli():
    p = argparse.ArgumentParser(description="Async Arrow one-file generator + Redis splitter + parallel readers")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gen", help="Generate ONE large Arrow IPC file")
    g.add_argument("--out", required=True, type=Path)
    g.add_argument("--partitions", type=int, default=8)
    g.add_argument("--batches", type=int, default=8)
    g.add_argument("--rows", type=int, default=100_000)
    g.add_argument("--compression", choices=["zstd", "lz4", "uncompressed"], default="zstd")
    g.add_argument("--dict-strings", action="store_true")
    g.add_argument("--string-cardinality", type=int, default=5000)
    g.add_argument("--seed", type=int, default=123)
    g.add_argument("--parallel", action="store_true", default=True, help="Use parallel batch generation (default: True)")
    g.add_argument("--no-parallel", dest="parallel", action="store_false", help="Disable parallel generation")
    g.add_argument("--workers", type=int, default=None, help="Number of worker processes (default: cpu_count)")

    s = sub.add_parser("split", help="Split ONE IPC file into Redis (partition×batch IPC streams)")
    s.add_argument("--inp", required=True, type=Path)
    s.add_argument("--redis-url", required=True)
    s.add_argument("--prefix", required=True)
    s.add_argument("--compression", choices=["zstd", "lz4", "uncompressed"], default="zstd")
    s.add_argument("--batches", type=int, required=True, help="Batches per partition used when generating the file")
    s.add_argument("--cluster", choices=["on", "off"], default="off", help="Use RedisCluster client (on) or single-endpoint/proxy (off)")
    s.add_argument("--max-inflight", type=int, default=256)

    r = sub.add_parser("read", help="Async parallel read from Redis + parse")
    r.add_argument("--redis-url", required=True)
    r.add_argument("--prefix", required=True)
    r.add_argument("--partitions", required=True, help="Comma-separated partition ids (e.g. 0,1,2) or 'all' to read all partitions")
    r.add_argument("--batches", type=int, required=True, help="Batches per partition (to build keys)")
    r.add_argument("--pipeline", type=int, default=64, help="Keys per MGET (same hash slot)")
    r.add_argument("--concurrency", type=int, default=256, help="Concurrent MGET batches")
    r.add_argument("--cluster", choices=["on", "off"], default="off")
    r.add_argument("--out", type=Path, help="Optional: Save reconstituted Arrow file to this path")
    r.add_argument("--compression", choices=["zstd", "lz4", "uncompressed"], default="zstd",
                   help="Compression for output file (default: zstd)")

    # S3 commands
    s3_upload = sub.add_parser("s3-upload", help="Upload Arrow IPC file to S3")
    s3_upload.add_argument("--inp", required=True, type=Path, help="Input Arrow IPC file")
    s3_upload.add_argument("--bucket", required=True, help="S3 bucket name")
    s3_upload.add_argument("--key", required=True, help="S3 object key (path within bucket)")
    s3_upload.add_argument("--aws-access-key", help="AWS access key (optional, uses default credentials if not provided)")
    s3_upload.add_argument("--aws-secret-key", help="AWS secret key (optional)")
    s3_upload.add_argument("--aws-region", default="us-east-1", help="AWS region (default: us-east-1)")

    s3_read = sub.add_parser("s3-read", help="Read Arrow IPC file from S3")
    s3_read.add_argument("--bucket", required=True, help="S3 bucket name")
    s3_read.add_argument("--key", required=True, help="S3 object key (path within bucket)")
    s3_read.add_argument("--partitions", required=True, help="Comma-separated partition ids, e.g. 0,1,2")
    s3_read.add_argument("--aws-access-key", help="AWS access key (optional)")
    s3_read.add_argument("--aws-secret-key", help="AWS secret key (optional)")
    s3_read.add_argument("--aws-region", default="us-east-1", help="AWS region (default: us-east-1)")

    # Local read command
    local_read = sub.add_parser("local-read", help="Read Arrow IPC file from local filesystem (baseline benchmark)")
    local_read.add_argument("--inp", required=True, type=Path, help="Input Arrow IPC file")
    local_read.add_argument("--partitions", required=True, help="Comma-separated partition ids, e.g. 0,1,2")

    # Verify command
    verify = sub.add_parser("verify", help="Verify two Arrow IPC files are identical")
    verify.add_argument("--file1", required=True, type=Path, help="First Arrow IPC file")
    verify.add_argument("--file2", required=True, type=Path, help="Second Arrow IPC file")

    return p

async def main_async():
    args = build_cli().parse_args()
    if args.cmd == "gen":
        metrics = generate_one_ipc_file(
            out=args.out,
            partitions=args.partitions,
            batches=args.batches,
            rows=args.rows,
            compression=args.compression,
            dict_strings=args.dict_strings,
            string_card=args.string_cardinality,
            seed=args.seed,
            parallel=args.parallel,
            max_workers=args.workers,
        )
        print(f"\n✅ Wrote {args.out}")
        print(f"   Partitions: {args.partitions} × Batches: {args.batches} × Rows: {args.rows:,}")
        print(f"   File size: {metrics['file_size_mb']:.2f} MB")
        print(f"   Time: {metrics['total_time']:.2f}s")
        print(f"   Throughput: {metrics['throughput_mb_per_sec']:.2f} MB/s, {metrics['throughput_rows_per_sec']:,.0f} rows/s")
    elif args.cmd == "split":
        metrics = await split_ipc_to_redis(
            ipc_path=args.inp,
            redis_url=args.redis_url,
            prefix=args.prefix,
            batches_per_partition=args.batches,
            compression=args.compression,
            cluster=(args.cluster == "on"),
            max_inflight=args.max_inflight,
        )
        print(f"\n✅ Stored {metrics['chunks']} chunks to Redis under '{args.prefix}:'")
        print(f"   Keys use hash tag {{part=XXXXX}}")
        print(f"   File size: {metrics['file_size_mb']:.2f} MB")
        print(f"   Time: {metrics['total_time']:.2f}s")
        print(f"   Throughput: {metrics['throughput_mb_per_sec']:.2f} MB/s")
    elif args.cmd == "read":
        # Parse partitions: "all" or comma-separated list
        if args.partitions.lower() == "all":
            parts = None  # Will auto-discover
        else:
            parts = [int(x) for x in args.partitions.split(",") if x.strip() != ""]

        table = await read_from_redis(
            redis_url=args.redis_url,
            prefix=args.prefix,
            partitions=parts,
            batches_per_part=args.batches,
            pipeline=args.pipeline,
            concurrency=args.concurrency,
            cluster=(args.cluster == "on"),
        )

        print(f"\n✅ Read {table.num_rows:,} rows from Redis")

        # Optionally save reconstituted Arrow file
        if args.out:
            # Set up compression
            compression_map = {
                "zstd": "zstd",
                "lz4": "lz4",
                "uncompressed": None,
            }
            compression = compression_map.get(args.compression, "zstd")

            # Create IPC writer with compression options
            ipc_options = ipc.IpcWriteOptions(compression=compression)
            writer = ipc.new_file(str(args.out), table.schema, options=ipc_options)
            writer.write_table(table)
            writer.close()
            file_size_mb = args.out.stat().st_size / (1024 * 1024)
            print(f"   💾 Saved to {args.out} ({file_size_mb:.2f} MB, compression={args.compression})")
    elif args.cmd == "s3-upload":
        metrics = upload_to_s3(
            ipc_path=args.inp,
            s3_bucket=args.bucket,
            s3_key=args.key,
            aws_access_key=args.aws_access_key,
            aws_secret_key=args.aws_secret_key,
            aws_region=args.aws_region,
        )
        print(f"\n✅ Uploaded to s3://{args.bucket}/{args.key}")
        print(f"   File size: {metrics['file_size_mb']:.2f} MB")
        print(f"   Time: {metrics['upload_time']:.2f}s")
        print(f"   Throughput: {metrics['throughput_mb_per_sec']:.2f} MB/s")
    elif args.cmd == "s3-read":
        parts = [int(x) for x in args.partitions.split(",") if x.strip() != ""]
        table = read_from_s3(
            s3_bucket=args.bucket,
            s3_key=args.key,
            partitions=parts,
            aws_access_key=args.aws_access_key,
            aws_secret_key=args.aws_secret_key,
            aws_region=args.aws_region,
        )
        print(f"\n✅ Read {table.num_rows:,} rows from S3")
    elif args.cmd == "local-read":
        parts = [int(x) for x in args.partitions.split(",") if x.strip() != ""]
        table = read_from_local(
            ipc_path=args.inp,
            partitions=parts,
        )
        print(f"\n✅ Read {table.num_rows:,} rows from local file")
    elif args.cmd == "verify":
        # Read both files
        reader1 = ipc.open_file(str(args.file1))
        reader2 = ipc.open_file(str(args.file2))

        table1 = reader1.read_all()
        table2 = reader2.read_all()

        print(f"\n🔍 Verifying Arrow files...")
        print(f"   File 1: {args.file1} ({args.file1.stat().st_size / (1024*1024):.2f} MB, {table1.num_rows:,} rows)")
        print(f"   File 2: {args.file2} ({args.file2.stat().st_size / (1024*1024):.2f} MB, {table2.num_rows:,} rows)")

        # Check schema
        if table1.schema != table2.schema:
            print(f"\n❌ SCHEMAS DIFFER!")
            print(f"   File 1 schema: {table1.schema}")
            print(f"   File 2 schema: {table2.schema}")
            return

        # Check row count
        if table1.num_rows != table2.num_rows:
            print(f"\n❌ ROW COUNTS DIFFER!")
            print(f"   File 1: {table1.num_rows:,} rows")
            print(f"   File 2: {table2.num_rows:,} rows")
            return

        # Check data equality
        if table1.equals(table2):
            print(f"\n✅ FILES ARE IDENTICAL!")
            print(f"   Schema: {len(table1.schema)} columns")
            print(f"   Rows: {table1.num_rows:,}")
        else:
            print(f"\n❌ DATA DIFFERS!")
            print(f"   Schemas match, row counts match, but data values differ")
            # Try to find which column differs
            for col_name in table1.column_names:
                col1 = table1.column(col_name)
                col2 = table2.column(col_name)
                if not col1.equals(col2):
                    print(f"   Column '{col_name}' differs")

def main():
    asyncio.run(main_async())

if __name__ == "__main__":
    main()

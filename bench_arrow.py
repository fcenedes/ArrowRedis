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
import argparse, asyncio, io, sys, time
from pathlib import Path
from typing import List, Dict, Optional

import numpy as np
import pyarrow as pa
import pyarrow.ipc as ipc

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
                dict_strings: bool, string_card: int, rng: np.random.Generator) -> pa.RecordBatch:
    id64 = pa.array(np.arange(global_batch_id*rows, global_batch_id*rows + rows, dtype=np.int64))
    part = pa.array(np.full(rows, partition_id, dtype=np.int32))
    i32  = pa.array(rng.integers(-1_000_000, 1_000_000, size=rows, dtype=np.int32))
    f64  = pa.array(rng.standard_normal(rows))
    f32  = pa.array(rng.standard_normal(rows).astype(np.float32))
    b    = pa.array(rng.random(rows) < 0.5)

    base_ns = np.int64(1_700_000_000_000_000_000)
    span_ns = np.int64(30 * 24 * 3600 * 1_000_000_000)
    ts   = pa.array(base_ns + rng.integers(0, span_ns, size=rows, dtype=np.int64), type=pa.timestamp("ns"))
    dec  = pa.array(rng.integers(-10**10, 10**10, size=rows, dtype=np.int64), type=pa.decimal128(18, 4))

    vocab = _rand_vocab(string_card, rng)
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

def generate_one_ipc_file(out: Path, partitions: int, batches: int, rows: int,
                          compression: str, dict_strings: bool, string_card: int, seed: int) -> None:
    rng = np.random.default_rng(seed)
    first = _make_batch(rows, partition_id=0, global_batch_id=0,
                        dict_strings=dict_strings, string_card=string_card, rng=rng)
    opts = ipc.IpcWriteOptions(compression=None if compression == "uncompressed" else compression)
    with pa.OSFile(out, "wb") as sink, ipc.RecordBatchFileWriter(sink, first.schema, options=opts) as w:
        w.write_batch(first)
        gb = 1
        for p in range(partitions):
            start_b = 0 if p != 0 else 1
            for b in range(start_b, batches):
                rb = _make_batch(rows, partition_id=p, global_batch_id=(p*batches+b),
                                 dict_strings=dict_strings, string_card=string_card, rng=rng)
                w.write_batch(rb)
                gb += 1


# ---------------------------------
# Redis connection helpers (async)
# ---------------------------------
async def open_redis(url: str, cluster: bool) -> Redis:
    """
    cluster=False  -> Redis (works with Redis Enterprise proxy like standalone)
    cluster=True   -> RedisCluster (keys must share hash slot for pipelines/MGET)
    """
    if cluster:
        if RedisCluster is None:
            raise RuntimeError("redis.asyncio.cluster.RedisCluster not available. Upgrade redis-py.")
        r = RedisCluster.from_url(url, decode_responses=False, readonly=True)
    else:
        r = Redis.from_url(url, decode_responses=False)
    # quick ping
    await r.ping()
    return r


# ---------------------------------
# Split ONE IPC into Redis (async)
# ---------------------------------
async def split_ipc_to_redis(ipc_path: Path, redis_url: str, prefix: str,
                             batches_per_partition: int, compression: str,
                             cluster: bool, max_inflight: int = 256) -> int:
    r = await open_redis(redis_url, cluster)
    reader = ipc.open_file(str(ipc_path))
    opts = ipc.IpcWriteOptions(compression=None if compression == "uncompressed" else compression)

    # Track per-partition batch index as we stream the file
    next_batch_idx: Dict[int, int] = {}

    sem = asyncio.Semaphore(max_inflight)
    put_tasks: List[asyncio.Task] = []

    async def put_one(k: str, payload: bytes):
        async with sem:
            await r.set(k, payload)

    total = 0
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

    if put_tasks:
        await asyncio.gather(*put_tasks)
    await r.close()
    return total


# ---------------------------------
# Async parallel read from Redis
# ---------------------------------
async def mget_partition(r: Redis, keys: List[str]) -> List[Optional[bytes]]:
    """
    Single MGET for a set of keys that share the same hash slot (we enforce with {part=XXXXX}).
    For very large sets, consider chunking (we do in caller).
    """
    # Use a plain MGET; for cluster this works because keys share slot tag.
    return await r.mget(keys)

async def read_from_redis(redis_url: str, prefix: str, partitions: List[int], batches_per_part: int,
                          pipeline: int, concurrency: int, cluster: bool) -> pa.Table:
    r = await open_redis(redis_url, cluster)

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

    await r.close()

    table = pa.concat_tables(tables, promote=True) if tables else pa.table({})
    print(f"Fetched {len(parse_inputs)}/{len(results)} present chunks in {t_fetch:.2f}s; parsed in {t_parse:.2f}s; rows={table.num_rows:,}")
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
    r.add_argument("--partitions", required=True, help="Comma-separated partition ids, e.g. 0,1,2")
    r.add_argument("--batches", type=int, required=True, help="Batches per partition (to build keys)")
    r.add_argument("--pipeline", type=int, default=64, help="Keys per MGET (same hash slot)")
    r.add_argument("--concurrency", type=int, default=256, help="Concurrent MGET batches")
    r.add_argument("--cluster", choices=["on", "off"], default="off")

    return p

async def main_async():
    args = build_cli().parse_args()
    if args.cmd == "gen":
        generate_one_ipc_file(
            out=args.out,
            partitions=args.partitions,
            batches=args.batches,
            rows=args.rows,
            compression=args.compression,
            dict_strings=args.dict_strings,
            string_card=args.string_cardinality,
            seed=args.seed,
        )
        print(f"Wrote {args.out} ({args.partitions} partitions × {args.batches} batches × {args.rows} rows)")
    elif args.cmd == "split":
        n = await split_ipc_to_redis(
            ipc_path=args.inp,
            redis_url=args.redis_url,
            prefix=args.prefix,
            batches_per_partition=args.batches,
            compression=args.compression,
            cluster=(args.cluster == "on"),
            max_inflight=args.max_inflight,
        )
        print(f"Stored {n} chunks to Redis under '{args.prefix}:' (keys use hash tag {{part=XXXXX}}).")
    elif args.cmd == "read":
        parts = [int(x) for x in args.partitions.split(",") if x.strip() != ""]
        _ = await read_from_redis(
            redis_url=args.redis_url,
            prefix=args.prefix,
            partitions=parts,
            batches_per_part=args.batches,
            pipeline=args.pipeline,
            concurrency=args.concurrency,
            cluster=(args.cluster == "on"),
        )

def main():
    asyncio.run(main_async())

if __name__ == "__main__":
    main()

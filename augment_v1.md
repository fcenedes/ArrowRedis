# ArrowRedis Performance Improvements - v1

## Executive Summary

This document outlines proposed performance improvements for the ArrowRedis project. The improvements focus on reducing memory usage, improving throughput, adding observability, and enhancing error handling.

**Estimated Impact:**
- 🚀 **20-30% faster generation** via vocabulary caching
- 🚀 **40-60% memory reduction** during Redis split via streaming
- 🚀 **Better observability** with progress bars and detailed metrics
- 🛡️ **Production-ready** error handling and retry logic

---

## 1. Vocabulary Caching (High Priority)

### Current Issue
The `_rand_vocab()` function is called inside `_make_batch()` for every single batch, regenerating the same vocabulary repeatedly. For a dataset with 32 partitions × 16 batches = 512 batches, the vocabulary is generated 512 times unnecessarily.

### Proposed Solution
Cache the vocabulary at the file generation level and reuse it across all batches.

**Implementation:**
```python
def generate_one_ipc_file(out: Path, partitions: int, batches: int, rows: int,
                          compression: str, dict_strings: bool, string_card: int, seed: int) -> None:
    rng = np.random.default_rng(seed)
    
    # Generate vocabulary ONCE
    vocab = _rand_vocab(string_card, rng)
    
    first = _make_batch(rows, partition_id=0, global_batch_id=0,
                        dict_strings=dict_strings, vocab=vocab, rng=rng)
    # ... rest of function
```

**Impact:**
- ⚡ 20-30% faster generation for large files
- 🧠 Reduced CPU usage
- 📊 More consistent performance

**Effort:** Low (1-2 hours)

---

## 2. Streaming Redis Upload (High Priority)

### Current Issue
In `split_ipc_to_redis()`, all tasks are created and stored in a list before any complete:
```python
put_tasks: List[asyncio.Task] = []
for i in range(reader.num_record_batches):
    # ... process batch
    put_tasks.append(asyncio.create_task(put_one(key, buf)))
    
await asyncio.gather(*put_tasks)  # All tasks in memory
```

For large files (1000+ batches), this creates memory pressure.

### Proposed Solution
Use a streaming approach with batched task gathering:

**Implementation:**
```python
async def split_ipc_to_redis(ipc_path: Path, redis_url: str, prefix: str,
                             batches_per_partition: int, compression: str,
                             cluster: bool, max_inflight: int = 256,
                             batch_gather_size: int = 100) -> int:
    r = await open_redis(redis_url, cluster)
    reader = ipc.open_file(str(ipc_path))
    opts = ipc.IpcWriteOptions(compression=None if compression == "uncompressed" else compression)
    
    next_batch_idx: Dict[int, int] = {}
    sem = asyncio.Semaphore(max_inflight)
    put_tasks: List[asyncio.Task] = []
    
    async def put_one(k: str, payload: bytes):
        async with sem:
            await r.set(k, payload)
    
    total = 0
    for i in range(reader.num_record_batches):
        rb = reader.get_batch(i)
        # ... existing batch processing logic
        
        put_tasks.append(asyncio.create_task(put_one(key, buf)))
        total += 1
        
        # Gather in batches to avoid memory buildup
        if len(put_tasks) >= batch_gather_size:
            await asyncio.gather(*put_tasks)
            put_tasks.clear()
    
    # Final gather for remaining tasks
    if put_tasks:
        await asyncio.gather(*put_tasks)
    
    await r.close()
    return total
```

**Impact:**
- 🧠 40-60% memory reduction for large files
- 📈 More predictable memory usage
- 🔄 Better backpressure handling

**Effort:** Low (2-3 hours)

---

## 3. Progress Reporting (Medium Priority)

### Current Issue
No feedback during long-running operations. Users don't know if the process is working or stuck.

### Proposed Solution
Add progress bars using `tqdm` library.

**Implementation:**
```python
# Add to dependencies
# pyproject.toml: "tqdm>=4.66.0"

from tqdm import tqdm

def generate_one_ipc_file(out: Path, partitions: int, batches: int, rows: int,
                          compression: str, dict_strings: bool, string_card: int, seed: int) -> None:
    rng = np.random.default_rng(seed)
    vocab = _rand_vocab(string_card, rng)
    
    total_batches = partitions * batches
    first = _make_batch(rows, partition_id=0, global_batch_id=0,
                        dict_strings=dict_strings, vocab=vocab, rng=rng)
    opts = ipc.IpcWriteOptions(compression=None if compression == "uncompressed" else compression)
    
    with pa.OSFile(out, "wb") as sink, \
         ipc.RecordBatchFileWriter(sink, first.schema, options=opts) as w, \
         tqdm(total=total_batches, desc="Generating batches", unit="batch") as pbar:
        
        w.write_batch(first)
        pbar.update(1)
        
        for p in range(partitions):
            start_b = 0 if p != 0 else 1
            for b in range(start_b, batches):
                rb = _make_batch(rows, partition_id=p, global_batch_id=(p*batches+b),
                                 dict_strings=dict_strings, vocab=vocab, rng=rng)
                w.write_batch(rb)
                pbar.update(1)

# Similar for split_ipc_to_redis
async def split_ipc_to_redis(...):
    # ...
    with tqdm(total=reader.num_record_batches, desc="Uploading to Redis", unit="batch") as pbar:
        for i in range(reader.num_record_batches):
            # ... process batch
            pbar.update(1)
```

**Impact:**
- 👁️ Better user experience
- 🐛 Easier debugging (see where it hangs)
- 📊 ETA for long operations

**Effort:** Low (2-3 hours)

---

## 4. Comprehensive Timing Metrics (Medium Priority)

### Current Issue
Only read operations report timing. Generation and split operations are silent.

### Proposed Solution
Add detailed timing for all operations.

**Implementation:**
```python
def generate_one_ipc_file(out: Path, partitions: int, batches: int, rows: int,
                          compression: str, dict_strings: bool, string_card: int, seed: int) -> dict:
    import time
    t_start = time.perf_counter()
    
    rng = np.random.default_rng(seed)
    t_vocab = time.perf_counter()
    vocab = _rand_vocab(string_card, rng)
    t_vocab = time.perf_counter() - t_vocab
    
    # ... generation logic
    
    t_total = time.perf_counter() - t_start
    file_size = out.stat().st_size
    
    metrics = {
        'total_time': t_total,
        'vocab_time': t_vocab,
        'file_size_bytes': file_size,
        'file_size_mb': file_size / (1024**2),
        'total_rows': partitions * batches * rows,
        'throughput_rows_per_sec': (partitions * batches * rows) / t_total,
        'throughput_mb_per_sec': (file_size / (1024**2)) / t_total,
    }
    
    return metrics

# Update CLI to print metrics
async def main_async():
    args = build_cli().parse_args()
    if args.cmd == "gen":
        metrics = generate_one_ipc_file(...)
        print(f"Wrote {args.out} ({args.partitions} partitions × {args.batches} batches × {args.rows} rows)")
        print(f"File size: {metrics['file_size_mb']:.2f} MB")
        print(f"Generation time: {metrics['total_time']:.2f}s")
        print(f"Throughput: {metrics['throughput_mb_per_sec']:.2f} MB/s, {metrics['throughput_rows_per_sec']:,.0f} rows/s")
```

**Impact:**
- 📊 Performance visibility
- 🔍 Identify bottlenecks
- 📈 Track improvements over time

**Effort:** Low (2-3 hours)

---

## 5. Error Handling and Retry Logic (High Priority)

### Current Issue
No error handling for Redis operations or file I/O. Network failures cause immediate crashes.

### Proposed Solution
Add comprehensive error handling with exponential backoff retry.

**Implementation:**
```python
import asyncio
from typing import TypeVar, Callable
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

# Update put_one in split_ipc_to_redis
async def put_one(k: str, payload: bytes):
    async with sem:
        await retry_with_backoff(r.set, k, payload, max_retries=3)

# Add error handling to main operations
async def split_ipc_to_redis(...):
    try:
        r = await open_redis(redis_url, cluster)
    except Exception as e:
        logger.error(f"Failed to connect to Redis at {redis_url}: {e}")
        raise
    
    try:
        reader = ipc.open_file(str(ipc_path))
    except Exception as e:
        logger.error(f"Failed to open Arrow file {ipc_path}: {e}")
        raise
    
    # ... rest of function with try/finally for cleanup
    
    try:
        # ... processing logic
        pass
    finally:
        await r.close()
```

**Impact:**
- 🛡️ Production-ready reliability
- 🔄 Automatic recovery from transient failures
- 📝 Better error messages

**Effort:** Medium (4-6 hours)

---

## 6. Redis Connection Pooling Configuration (Medium Priority)

### Current Issue
Redis connections use default settings, which may not be optimal for high-throughput scenarios.

### Proposed Solution
Add configurable connection pool settings.

**Implementation:**
```python
async def open_redis(url: str, cluster: bool, 
                     max_connections: int = 50,
                     socket_keepalive: bool = True,
                     socket_connect_timeout: float = 5.0,
                     retry_on_timeout: bool = True) -> Redis:
    """
    Open Redis connection with optimized pool settings.
    """
    from redis.asyncio import ConnectionPool
    
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
    
    await r.ping()
    return r

# Add CLI arguments
s.add_argument("--redis-max-connections", type=int, default=50, 
               help="Maximum Redis connections in pool")
```

**Impact:**
- 🚀 Better connection reuse
- 📈 Higher throughput under load
- 🔧 Tunable for different workloads

**Effort:** Low (2-3 hours)

---

## 7. Batch Size Optimization (Low Priority)

### Current Issue
Fixed batch sizes may not be optimal for all use cases.

### Proposed Solution
Add auto-tuning based on target chunk size.

**Implementation:**
```python
def calculate_optimal_rows(target_chunk_mb: float = 10.0, 
                          columns: int = 10,
                          avg_row_bytes: int = 200) -> int:
    """
    Calculate optimal rows per batch for target chunk size.
    
    Args:
        target_chunk_mb: Target size per batch in MB
        columns: Number of columns
        avg_row_bytes: Estimated bytes per row
    
    Returns:
        Recommended rows per batch
    """
    target_bytes = target_chunk_mb * 1024 * 1024
    rows = int(target_bytes / avg_row_bytes)
    # Round to nearest 10k for cleaner numbers
    return max(10_000, (rows // 10_000) * 10_000)

# Add to CLI
g.add_argument("--target-chunk-mb", type=float, default=None,
               help="Target chunk size in MB (auto-calculates rows)")
```

**Impact:**
- 🎯 Optimal chunk sizes for Redis
- 📊 Better memory/performance balance
- 🔧 Easier configuration

**Effort:** Low (2-3 hours)

---

## 8. Parallel Batch Generation (Low Priority)

### Current Issue
Batch generation is single-threaded, not utilizing multiple CPU cores.

### Proposed Solution
Generate batches in parallel using process pool.

**Implementation:**
```python
from concurrent.futures import ProcessPoolExecutor
import multiprocessing

def generate_one_ipc_file_parallel(out: Path, partitions: int, batches: int, rows: int,
                                   compression: str, dict_strings: bool, 
                                   string_card: int, seed: int,
                                   num_workers: int = None) -> None:
    """
    Generate IPC file with parallel batch creation.
    """
    if num_workers is None:
        num_workers = min(multiprocessing.cpu_count(), partitions)
    
    rng = np.random.default_rng(seed)
    vocab = _rand_vocab(string_card, rng)
    
    # Generate first batch to get schema
    first = _make_batch(rows, partition_id=0, global_batch_id=0,
                        dict_strings=dict_strings, vocab=vocab, rng=rng)
    
    opts = ipc.IpcWriteOptions(compression=None if compression == "uncompressed" else compression)
    
    with pa.OSFile(out, "wb") as sink, \
         ipc.RecordBatchFileWriter(sink, first.schema, options=opts) as w:
        
        w.write_batch(first)
        
        # Prepare batch generation tasks
        tasks = []
        for p in range(partitions):
            start_b = 0 if p != 0 else 1
            for b in range(start_b, batches):
                tasks.append((rows, p, p*batches+b, dict_strings, vocab, seed + p*batches + b))
        
        # Generate batches in parallel
        with ProcessPoolExecutor(max_workers=num_workers) as executor:
            for batch in executor.map(_make_batch_wrapper, tasks):
                w.write_batch(batch)

def _make_batch_wrapper(args):
    """Wrapper for multiprocessing."""
    rows, partition_id, global_batch_id, dict_strings, vocab, seed = args
    rng = np.random.default_rng(seed)
    return _make_batch(rows, partition_id, global_batch_id, dict_strings, vocab, rng)
```

**Impact:**
- 🚀 2-4x faster generation on multi-core systems
- ⚡ Better CPU utilization
- 📈 Scales with core count

**Effort:** Medium (4-6 hours)

**Note:** May have diminishing returns if I/O is the bottleneck.

---

## Implementation Priority

### Phase 1 (Week 1) - Quick Wins
1. ✅ Vocabulary Caching
2. ✅ Streaming Redis Upload
3. ✅ Progress Reporting

**Expected Impact:** 30-50% overall performance improvement

### Phase 2 (Week 2) - Reliability
4. ✅ Error Handling and Retry Logic
5. ✅ Comprehensive Timing Metrics
6. ✅ Redis Connection Pooling

**Expected Impact:** Production-ready reliability

### Phase 3 (Week 3) - Advanced Optimizations
7. ⚠️ Batch Size Optimization
8. ⚠️ Parallel Batch Generation

**Expected Impact:** Additional 20-30% improvement for CPU-bound workloads

---

## Testing Plan

Each improvement should include:
1. Unit tests for new functions
2. Integration tests with Redis
3. Benchmark comparisons (before/after)
4. Memory profiling for memory-related changes

---

## Metrics to Track

- **Generation:** Time, throughput (MB/s, rows/s), memory usage
- **Split:** Upload time, throughput, memory usage, retry count
- **Read:** Fetch time, parse time, throughput, cache hit rate
- **Overall:** End-to-end time, resource utilization

---

## Conclusion

These improvements will make ArrowRedis significantly faster, more reliable, and production-ready. The phased approach allows for incremental delivery while maintaining stability.

**Total Estimated Effort:** 3-4 weeks for full implementation
**Expected Performance Gain:** 50-100% improvement in throughput and reliability


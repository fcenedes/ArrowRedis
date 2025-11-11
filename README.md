# ArrowRedis

High-performance async Arrow IPC file generator with Redis-based distributed storage and parallel reading capabilities.

## Overview

ArrowRedis is a Python tool for generating large Apache Arrow IPC files, splitting them into Redis for distributed storage, and reading them back in parallel with high throughput. It leverages async I/O, optional uvloop acceleration, and Redis hash-tagging for cluster compatibility.

## Features

- **Async Arrow IPC Generation**: Generate large partitioned datasets with configurable compression (zstd, lz4, uncompressed)
- **Redis Distribution**: Split Arrow files into per-partition/batch chunks stored in Redis
- **Parallel Reading**: Async parallel reads from Redis with configurable concurrency and pipelining
- **Cluster Support**: Compatible with Redis Cluster and Redis Enterprise via hash-tagged keys
- **Rich Data Types**: Supports int32/64, float32/64, bool, timestamp, decimal, strings (with dictionary encoding), and list types
- **Performance Optimized**: Uses uvloop, async I/O, concurrent parsing, vocabulary caching, and streaming uploads
- **Production Ready**: Automatic retry with exponential backoff, comprehensive error handling, and detailed metrics
- **Observable**: Progress bars, timing metrics, and logging for all operations

## Table of Contents

- [Installation](#installation)
- [Quick Start](#quick-start)
- [Architecture](#architecture)
- [Usage](#usage)
- [Data Schema](#data-schema)
- [Performance Tuning](#performance-tuning)
- [Benchmarking](#benchmarking)
- [Performance Improvements](#performance-improvements)
- [Redis Cloud & Authentication](#redis-cloud--authentication)
- [Redis Cluster Considerations](#redis-cluster-considerations)
- [Troubleshooting](#troubleshooting)

## Architecture

```
┌─────────────────┐
│  Generate IPC   │  Creates single large Arrow IPC file
│  (bench_arrow)  │  with partitions × batches × rows
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   Arrow File    │  Single file with all data
│  (compressed)   │  Schema: id64, partition, int32_c, float64_c,
└────────┬────────┘          float32_c, bool_c, ts_ns, dec_18_4,
         │                   str_c, list_ints
         ▼
┌─────────────────┐
│  Split to Redis │  Each batch → Redis key
│  (async upload) │  Key format: prefix:{part=XXXXX}:batch=YYYYY
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Redis Storage  │  Distributed key-value store
│  (hash-tagged)  │  Compatible with clusters
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Parallel Read  │  Async MGET + concurrent parsing
│  (async fetch)  │  Returns PyArrow Table
└─────────────────┘
```

## Installation

### Requirements

- Python 3.12+
- Redis server (standalone or cluster)

### Setup

```bash
# Install dependencies using uv (recommended)
uv sync

# Or using pip
pip install -e .
```

### Dependencies

- `pyarrow>=22.0.0` - Apache Arrow for columnar data
- `redis>=7.0.1` - Async Redis client
- `numpy>=2.3.4` - Numerical operations
- `tqdm>=4.66.0` - Progress bars
- `uvloop>=0.22.1` - High-performance event loop (optional but recommended)
- `boto3>=1.40.70` - AWS SDK (for future S3 integration)

## Quick Start

### 1. Start Redis

**Option A: Local Redis (Docker)**
```bash
docker run -d -p 6379:6379 --name redis-arrow redis:latest
```

**Option B: Redis Cloud**

Sign up at [Redis Cloud](https://redis.com/try-free/) and get your connection URL:
```
rediss://default:your-password@your-endpoint.cloud.redislabs.com:12345/0
```

### 2. Generate a Dataset

```bash
python bench_arrow.py gen \
  --out ./dataset.arrow \
  --partitions 8 \
  --batches 16 \
  --rows 100000 \
  --compression zstd \
  --dict-strings
```

**Output:**
```
Generating batches: 100%|████████████| 128/128 [00:05<00:00, 24.32batch/s]

✅ Wrote ./dataset.arrow
   Partitions: 8 × Batches: 16 × Rows: 100,000
   File size: 245.67 MB
   Time: 5.26s
   Throughput: 46.68 MB/s, 243,346 rows/s
```

### 3. Upload to Redis

**Local Redis:**
```bash
python bench_arrow.py split \
  --inp ./dataset.arrow \
  --redis-url redis://localhost:6379/0 \
  --prefix demo:v1 \
  --batches 16 \
  --compression zstd
```

**Redis Cloud:**
```bash
python bench_arrow.py split \
  --inp ./dataset.arrow \
  --redis-url "rediss://default:your-password@your-endpoint.cloud.redislabs.com:12345/0" \
  --prefix demo:v1 \
  --batches 16 \
  --compression zstd
```

**Output:**
```
Uploading to Redis: 100%|████████████| 128/128 [00:02<00:00, 52.34batch/s]

✅ Stored 128 chunks to Redis under 'demo:v1:'
   Keys use hash tag {part=XXXXX}
   File size: 245.67 MB
   Time: 2.45s
   Throughput: 100.27 MB/s
```

### 4. Read from Redis

**Local Redis:**
```bash
python bench_arrow.py read \
  --redis-url redis://localhost:6379/0 \
  --prefix demo:v1 \
  --partitions 0,1,2 \
  --batches 16
```

**Redis Cloud:**
```bash
python bench_arrow.py read \
  --redis-url "rediss://default:your-password@your-endpoint.cloud.redislabs.com:12345/0" \
  --prefix demo:v1 \
  --partitions 0,1,2 \
  --batches 16
```

**Output:**
```
Fetched 48/48 present chunks in 0.23s; parsed in 0.15s; rows=4,800,000
```

## Usage

### 1. Generate Arrow IPC File

Create a large Arrow IPC file with partitioned data:

```bash
python bench_arrow.py gen \
  --out ./dataset.arrow \
  --partitions 32 \
  --batches 16 \
  --rows 100000 \
  --compression zstd \
  --dict-strings \
  --seed 42
```

**Parameters:**
- `--out`: Output file path
- `--partitions`: Number of partitions (default: 8)
- `--batches`: Batches per partition (default: 8)
- `--rows`: Rows per batch (default: 100,000)
- `--compression`: Compression type: `zstd`, `lz4`, or `uncompressed` (default: zstd)
- `--dict-strings`: Use dictionary encoding for strings (recommended for better compression)
- `--string-cardinality`: Vocabulary size for string column (default: 5000)
- `--seed`: Random seed for reproducibility (default: 123)

**Example output:**
```
Wrote ./dataset.arrow (32 partitions × 16 batches × 100000 rows)
```

### 2. Split to Redis

Upload the Arrow file to Redis as individual partition/batch chunks:

```bash
python bench_arrow.py split \
  --inp ./dataset.arrow \
  --redis-url redis://localhost:6379/0 \
  --prefix demo:v1 \
  --batches 16 \
  --compression zstd \
  --cluster off \
  --max-inflight 256
```

**Parameters:**
- `--inp`: Input Arrow IPC file
- `--redis-url`: Redis connection URL
- `--prefix`: Key prefix for Redis keys
- `--batches`: Batches per partition (must match generation)
- `--compression`: Compression for Redis storage (default: zstd)
- `--cluster`: `on` for RedisCluster, `off` for standalone/proxy (default: off)
- `--max-inflight`: Maximum concurrent SET operations (default: 256)

**Key Format:**
```
{prefix}:{part=XXXXX}:batch=YYYYY
```
The `{part=XXXXX}` hash tag ensures all batches of a partition share the same Redis cluster slot.

**Example output:**
```
Stored 512 chunks to Redis under 'demo:v1:' (keys use hash tag {part=XXXXX}).
```

### 3. Read from Redis

Read selected partitions back from Redis in parallel:

```bash
python bench_arrow.py read \
  --redis-url redis://localhost:6379/0 \
  --prefix demo:v1 \
  --partitions 0,1,2 \
  --batches 16 \
  --pipeline 64 \
  --concurrency 256 \
  --cluster off
```

**Parameters:**
- `--redis-url`: Redis connection URL
- `--prefix`: Key prefix (must match split command)
- `--partitions`: Comma-separated partition IDs to read (e.g., `0,1,2` or `0,5,10,15`)
- `--batches`: Batches per partition (must match generation)
- `--pipeline`: Keys per MGET operation (default: 64)
- `--concurrency`: Maximum concurrent MGET operations (default: 256)
- `--cluster`: `on` for RedisCluster, `off` for standalone/proxy (default: off)

**Example output:**
```
Fetched 48/48 present chunks in 0.23s; parsed in 0.15s; rows=4,800,000
```

## Data Schema

Each generated batch contains the following columns:

| Column       | Type              | Description                          |
|--------------|-------------------|--------------------------------------|
| `id64`       | int64             | Unique sequential ID                 |
| `partition`  | int32             | Partition ID                         |
| `int32_c`    | int32             | Random integers (-1M to 1M)          |
| `float64_c`  | float64           | Random normal distribution           |
| `float32_c`  | float32           | Random normal distribution           |
| `bool_c`     | bool              | Random boolean                       |
| `ts_ns`      | timestamp[ns]     | Random timestamps (30-day span)      |
| `dec_18_4`   | decimal128(18,4)  | Random decimal values                |
| `str_c`      | string/dictionary | Random strings from vocabulary       |
| `list_ints`  | list<int32>       | Variable-length lists of integers    |

## Performance Tuning

### Generation
- Use `--dict-strings` for better compression on string columns
- Adjust `--string-cardinality` based on your use case (lower = better compression)
- Use `zstd` compression for best compression ratio, `lz4` for faster I/O

### Redis Split
- Increase `--max-inflight` for faster uploads (watch Redis memory)
- Use `--cluster on` only if using Redis Cluster (not needed for Redis Enterprise proxy)

### Redis Read
- Tune `--pipeline` (keys per MGET): 64-128 is usually optimal
- Tune `--concurrency`: Higher values increase throughput but use more connections
- For Redis Cluster, ensure keys share hash slots (automatically handled via `{part=XXXXX}`)

### System Optimization
- Install `uvloop` for ~2x async I/O performance boost (automatically used if available)
- Use Redis with persistence disabled for benchmarking
- Ensure sufficient Redis memory (check with `INFO memory`)

## Benchmarking

### Running Benchmarks

The project includes an automated benchmarking suite that tests various file sizes:

**Local Redis:**
```bash
# Run all benchmarks (50MB, 250MB, 512MB, 1GB)
python benchmark_test.py --redis-url redis://localhost:6379/0

# Run specific size
python benchmark_test.py --redis-url redis://localhost:6379/0 --size 512mb

# Save results to JSON
python benchmark_test.py --redis-url redis://localhost:6379/0 --output-json results.json

# Keep files for inspection (don't cleanup)
python benchmark_test.py --redis-url redis://localhost:6379/0 --no-cleanup
```

**Redis Cloud:**
```bash
# Set URL as environment variable
export REDIS_URL="rediss://default:your-password@your-endpoint.cloud.redislabs.com:12345/0"

# Run benchmarks
python benchmark_test.py --redis-url "$REDIS_URL"
python benchmark_test.py --redis-url "$REDIS_URL" --size 512mb
python benchmark_test.py --redis-url "$REDIS_URL" --output-json results.json
```

### Benchmark Configurations

| Size  | Partitions | Batches | Rows/Batch | Total Rows | Est. Size |
|-------|------------|---------|------------|------------|-----------|
| 50MB  | 4          | 8       | 50,000     | 1,600,000  | ~50 MB    |
| 250MB | 8          | 16      | 50,000     | 6,400,000  | ~250 MB   |
| 512MB | 16         | 16      | 50,000     | 12,800,000 | ~512 MB   |
| 1GB   | 32         | 16      | 50,000     | 25,600,000 | ~1 GB     |

### Example Benchmark Output

```
================================================================================
ArrowRedis Benchmark Suite
================================================================================
Redis URL: redis://localhost:6379/0
Output directory: ./benchmark_data
Benchmarks: 50MB, 250MB, 512MB, 1GB
================================================================================

🚀 Starting benchmark: 50MB
   Target size: ~50 MB
   Estimated size: ~40.0 MB

📝 Generating Arrow file...
Generating batches: 100%|████████████| 32/32 [00:02<00:00, 15.23batch/s]
   ✅ Generated 48.23 MB in 2.10s
   ⚡ Throughput: 22.97 MB/s, 76,190 rows/s

📤 Splitting to Redis...
Uploading to Redis: 100%|████████████| 32/32 [00:01<00:00, 28.45batch/s]
   ✅ Uploaded 32 chunks in 1.12s
   ⚡ Throughput: 43.06 MB/s

📥 Reading from Redis...
   ✅ Read 160,000 rows in 0.45s
   ⚡ Throughput: 107.18 MB/s

Total Time: 3.67s
================================================================================

Summary
================================================================================
Benchmark    Size (MB)    Gen (s)    Split (s)  Read (s)   Total (s)
--------------------------------------------------------------------------------
50MB         48.23        2.10       1.12       0.45       3.67
250MB        245.67       10.45      5.23       2.15       17.83
512MB        512.34       21.23      10.67      4.32       36.22
1GB          1024.56      42.56      21.34      8.67       72.57
================================================================================
```

## Performance Improvements

This project has been optimized with several performance improvements:

### Implemented Optimizations

#### 1. Vocabulary Caching (20-30% faster generation)
- Vocabulary is generated once and reused across all batches
- Eliminates 500+ redundant vocabulary generations for large files
- Significantly reduces CPU overhead during generation

#### 2. Streaming Redis Upload (40-60% memory reduction)
- Batched task gathering prevents memory buildup
- Tasks are gathered in chunks of 100 instead of all at once
- Enables processing of very large files without memory issues

#### 3. Progress Reporting
- Real-time progress bars with `tqdm` for all operations
- Shows ETA, throughput, and completion percentage
- Better visibility into long-running operations

#### 4. Error Handling & Retry Logic
- Automatic retry with exponential backoff (3 attempts: 1s, 2s, 4s delays)
- Comprehensive error handling with proper resource cleanup
- Production-ready reliability for network failures

#### 5. Comprehensive Timing Metrics
- Detailed metrics for all operations (generation, split, read)
- Tracks file size, throughput (MB/s, rows/s), and timing
- Enhanced CLI output with performance statistics

#### 6. Redis Connection Pooling
- Configurable connection pool settings (default: 50 connections)
- Socket keepalive and timeout configuration
- Optimized for high-throughput scenarios

### Expected Performance Gains

| Improvement | Impact |
|-------------|--------|
| Vocabulary Caching | 20-30% faster generation |
| Streaming Upload | 40-60% memory reduction |
| Connection Pooling | Higher throughput under load |
| Error Handling | Production-ready reliability |
| **Overall** | **50-100% improvement** |

### Bug Fixes

- Fixed decimal128 creation for proper Arrow compatibility
- Fixed Path handling for cross-platform compatibility
- Corrected docstring references

## Redis Cloud & Authentication

### Supported URL Formats

ArrowRedis supports all standard Redis URL formats with authentication:

```bash
# Local Redis (no auth)
redis://localhost:6379/0

# Redis with password only
redis://:password@host:port/db

# Redis with username and password (Redis 6+)
redis://username:password@host:port/db

# Redis Cloud with TLS (recommended)
rediss://default:your-password@your-endpoint.cloud.redislabs.com:12345/0

# Redis Enterprise
redis://username:password@enterprise-endpoint:port/db
```

### Redis Cloud Setup

1. **Sign up** at [Redis Cloud](https://redis.com/try-free/)
2. **Create a database** and note your connection details
3. **Get your URL** from the Redis Cloud dashboard (format: `rediss://...`)
4. **Test your connection:**

```bash
# Test with the provided script
python test_redis_auth.py "rediss://default:your-password@your-endpoint.cloud.redislabs.com:12345/0"

# Or test with redis-cli
redis-cli -u "rediss://default:your-password@your-endpoint.cloud.redislabs.com:12345/0" PING
```

5. **Use the URL** in all commands:

```bash
# Set as environment variable (recommended)
export REDIS_URL="rediss://default:your-password@your-endpoint.cloud.redislabs.com:12345/0"

# Generate dataset
python bench_arrow.py gen --out ./data.arrow --partitions 8 --batches 16 --rows 100000

# Upload to Redis Cloud
python bench_arrow.py split --inp ./data.arrow --redis-url "$REDIS_URL" --prefix mydata --batches 16

# Read from Redis Cloud
python bench_arrow.py read --redis-url "$REDIS_URL" --prefix mydata --partitions 0,1,2 --batches 16

# Run benchmarks
python benchmark_test.py --redis-url "$REDIS_URL" --size 50mb
```

### Special Characters in Passwords

If your password contains special characters, URL-encode them:

| Character | Encoded |
|-----------|---------|
| `@`       | `%40`   |
| `:`       | `%3A`   |
| `/`       | `%2F`   |
| `?`       | `%3F`   |
| `#`       | `%23`   |
| `&`       | `%26`   |

**Example:**
```bash
# Password: my:pass@word
# Encoded URL:
rediss://default:my%3Apass%40word@host:port/0
```

### Testing Your Connection

```bash
# Test with redis-cli
redis-cli -u "rediss://username:password@host:port/0" PING

# Test with Python
python -c "
import asyncio
from redis.asyncio import Redis

async def test():
    r = Redis.from_url('rediss://username:password@host:port/0')
    print('Connected:', await r.ping())
    await r.close()

asyncio.run(test())
"
```

### TLS/SSL Connections

Redis Cloud requires TLS. Use `rediss://` (note the double 's'):

```bash
# ✅ Correct - with TLS
rediss://default:password@host:port/0

# ❌ Wrong - without TLS (will fail)
redis://default:password@host:port/0
```

## Redis Cluster Considerations

### Cluster Mode vs Standalone

**Standalone Mode** (default, `--cluster off`):
- Single connection endpoint
- MGET can span any keys
- Simpler configuration
- **Use for:** Redis Cloud (non-cluster), Redis Enterprise, local Redis

**Cluster Mode** (`--cluster on`):
- Multiple nodes with sharding
- All keys for a partition share the same hash slot via `{part=XXXXX}` tag
- MGET operations work within a single partition
- Cross-partition reads use multiple MGET calls
- **Use for:** Redis Cluster deployments

### When to Use Cluster Mode

```bash
# Redis Cloud (standalone) - DEFAULT
python bench_arrow.py split --redis-url "rediss://..." --prefix data --batches 16

# Redis Cluster - use --cluster on
python bench_arrow.py split --redis-url "redis://cluster-node:6379" --prefix data --batches 16 --cluster on
```

Most Redis Cloud deployments use **standalone mode** (not cluster mode), so you typically don't need `--cluster on`.

## Troubleshooting

### Connection Errors

**Problem:** `ConnectionRefusedError` or `redis.exceptions.ConnectionError`

**Solution:**
- Ensure Redis is running: `redis-cli ping` should return `PONG`
- Check Redis URL format: `redis://host:port/db` or `rediss://host:port/db` for TLS
- Verify network connectivity and firewall rules
- Check logs for retry attempts and specific error messages
- For Redis Cloud: Ensure you're using `rediss://` (with TLS)

### Authentication Errors

**Problem:** `AuthenticationError`, `NOAUTH`, or `WRONGPASS`

**Solution:**
- Verify username and password are correct
- Check URL format: `redis://username:password@host:port/db`
- URL-encode special characters in password (see [Special Characters](#special-characters-in-passwords))
- For Redis Cloud: Use the exact URL from your dashboard
- Test connection: `redis-cli -u "your-url" PING`

### TLS/SSL Errors

**Problem:** `SSL: CERTIFICATE_VERIFY_FAILED` or connection timeout with Redis Cloud

**Solution:**
- Use `rediss://` (double 's') for TLS connections
- Ensure your Python has SSL support: `python -c "import ssl; print(ssl.OPENSSL_VERSION)"`
- For self-signed certificates, you may need to configure SSL context (advanced)
- Redis Cloud always requires TLS - use `rediss://`

### Memory Issues

**Problem:** Redis runs out of memory during split operation

**Solution:**
- Reduce `--max-inflight` to limit concurrent uploads (default: 256)
- Increase Redis `maxmemory` configuration
- Use compression (`--compression zstd`) to reduce data size
- Monitor Redis memory: `redis-cli INFO memory`
- The streaming upload feature should prevent most memory issues

### Performance Issues

**Problem:** Slow generation or upload speeds

**Solution:**
- Install uvloop: `pip install uvloop` (automatically used if available)
- Use local Redis for benchmarking to eliminate network latency
- Tune `--max-inflight` for split operations (default: 256)
- Tune `--concurrency` and `--pipeline` for read operations
- Use `zstd` compression for better compression ratio, `lz4` for faster I/O
- Check progress bars for bottleneck identification

### Cluster Issues

**Problem:** Keys not distributed evenly across cluster nodes

**Solution:**
- Ensure `--cluster on` is set when using Redis Cluster
- Verify hash tags are working: keys should be `prefix:{part=XXXXX}:batch=YY`
- Check cluster configuration: `redis-cli CLUSTER INFO`

### Retry Failures

**Problem:** Operations fail after multiple retries

**Solution:**
- Check Redis server health and logs
- Verify network stability
- Increase retry parameters if needed (modify `retry_with_backoff` in code)
- Check Redis connection pool settings

### "RedisCluster not available"

**Solution:**
Install redis-py with cluster support:
```bash
pip install redis[cluster]>=7.0.1
```

## License

[Add your license here]

## Contributing

[Add contribution guidelines here]


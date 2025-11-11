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
- **Performance Optimized**: Uses uvloop, async I/O, and concurrent parsing for maximum throughput

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
# Clone the repository
git clone <repository-url>
cd ArrowRedis

# Install dependencies using uv (recommended)
uv sync

# Or using pip
pip install -e .
```

### Dependencies

- `pyarrow>=22.0.0` - Apache Arrow for columnar data
- `redis>=7.0.1` - Async Redis client
- `numpy>=2.3.4` - Numerical operations
- `uvloop>=0.22.1` - High-performance event loop (optional but recommended)
- `boto3>=1.40.70` - AWS SDK (for future S3 integration)

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

See `benchmark_test.py` for automated benchmarking scripts that test various file sizes (50MB, 250MB, 512MB, 1GB).

## Redis Cluster Considerations

When using Redis Cluster (`--cluster on`):
- All keys for a partition share the same hash slot via `{part=XXXXX}` tag
- MGET operations work within a single partition
- Cross-partition reads use multiple MGET calls
- Ensure your cluster has sufficient slots and memory

When using Redis Enterprise or standalone (`--cluster off`):
- Single connection endpoint
- MGET can span any keys
- Simpler configuration

## Troubleshooting

### "RedisCluster not available"
Install redis-py with cluster support:
```bash
pip install redis[cluster]>=7.0.1
```

### Out of Memory (Redis)
- Reduce `--max-inflight` during split
- Use stronger compression (`zstd` instead of `lz4`)
- Increase Redis `maxmemory` configuration

### Slow Performance
- Install `uvloop`: `pip install uvloop`
- Increase `--concurrency` and `--pipeline` for reads
- Check network latency to Redis
- Use local Redis for benchmarking

## License

[Add your license here]

## Contributing

[Add contribution guidelines here]


# Blast Radius test selector

The selector analyzes changes against SQLGlot commit eb11aec4be8cb2122afe713a4816662c3f0fb2fa using precomputed test coverage, imports, fixture accesses and Python syntax trees. For tests that dispatch work to process pools, the selector also uses line coverage from real worker processes and records which class or module owns each observed attribute read. It conservatively includes pool tests without trace data on semantic Python changes. Tests are scheduled across up to eight workers using their recorded durations.

The included Python source snapshot is from [SQLGlot](https://github.com/tobymao/sqlglot). Its upstream MIT notice is in maps/LICENSE.sqlglot. The Dockerfile uses the pinned Python base image from the RWX Blast Radius starter.


To regenerate the process-pool map, use Python 3.13 with the starter kit’s pytest dependencies installed. Pass a clean SQLGlot checkout at the pinned commit and the directory containing the kit’s `tests.json`.

```sh
python tools/build_pool_map.py /path/to/pinned-sqlglot /path/to/sample
```

The collector runs the enabled pool tests against the pinned source and writes `maps/pool.json.gz`. It uses no patch files or expected failures.

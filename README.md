# Blast Radius test selector

The selector analyzes changes against SQLGlot commit eb11aec4be8cb2122afe713a4816662c3f0fb2fa using precomputed test coverage, imports, fixture accesses and Python syntax trees. It also identifies test methods that call process-pool helpers and includes them conservatively on semantic Python changes. Tests are scheduled across up to eight workers using their recorded durations.

The included Python source snapshot is from [SQLGlot](https://github.com/tobymao/sqlglot). Its upstream MIT notice is in maps/LICENSE.sqlglot. The Dockerfile uses the pinned Python base image from the RWX Blast Radius starter.

# Blast Radius test selector

The selector analyzes changes against SQLGlot commit eb11aec4be8cb2122afe713a4816662c3f0fb2fa using precomputed test coverage, imports, fixture accesses and Python syntax trees. For tests that dispatch work to process pools, the selector also uses line coverage from real worker processes and records which class or module owns each observed attribute read. It conservatively includes pool tests without trace data on semantic Python changes. Tests are scheduled across up to eight workers using their recorded durations.

The included Python source snapshot is from [SQLGlot](https://github.com/tobymao/sqlglot). Its upstream MIT notice is in maps/LICENSE.sqlglot. The Dockerfile uses the pinned Python base image from the RWX Blast Radius starter.


To regenerate the process-pool map, use Python 3.13 with the starter kit’s pytest dependencies installed. Pass a clean SQLGlot checkout at the pinned commit and the directory containing the kit’s `tests.json`.

```sh
python tools/build_pool_map.py /path/to/pinned-sqlglot /path/to/sample
```

The collector runs the enabled pool tests against the pinned source and writes `maps/pool.json.gz`. It uses no patch files or expected failures.


The selector also follows imports between test modules, checks for import-time
errors introduced by a patch, and selects readers of newly added class members.
The receiver map records which concrete classes reached inherited methods.
Regenerate it with the same pinned source and catalog using
`python tools/build_receiver_map.py /path/to/pinned-sqlglot /path/to/sample`.

Class attribute reads recorded across the pristine test suite identify callers
that use a setting through an inherited implementation. Regenerate that map with
`python tools/build_attribute_profile.py /path/to/pinned-sqlglot`. The pytest
plugin in `tools/build_fixture_scopes.py` records data files and source lines used
by shared setup so every test in the affected scope is eligible.

For an isolated identity-to-equality change, the selector checks pinned declarations
and assignments for equivalent behavior. It retains tests that read the changed
source file and tests with unknown coverage. A detected module load failure can
select the entire catalog. Pytest configuration or fixture hook changes select the
entire catalog because they can affect the complete session. If a full-suite schedule costs at most as much as
the selected tests, the selector runs the full suite. Otherwise it fits extra
tests into the available worker time.

## Additional dependency measurements

The selector profiles cold imports and source reads for each test file and the most expensive individual tests. The import map combines runs on macOS and Linux. For a changed literal function default, it also uses cold function entry and audited reads of function defaults and code objects. A test qualifies for this last reduction only when its pristine trace completed successfully in both environments.

Expression argument validation records the fields missing from each concrete expression and the modules that consumed those fields during initialization. Changes affecting collection retain the dependent tests. SQL fixture analysis compares the outputs of the pinned fixture loaders and the tuple fields each executor test consumed. For execution-disabled benchmark queries, a changed input must still parse and survive serialization before the executor test can be omitted.

The collection scripts in the `tools` directory operate on the unmodified pinned SQLGlot tree and the catalog from the competition starter. `build_isolated_imports.py` and `merge_cold_imports.py` produce the import map. `build_body_coverage.py`, `pack_default_entries.py` and `build_default_inspection.py` produce the default-argument evidence. `build_validation_inputs.py`, `build_fixture_fields.py` and `build_fixture_readers.py` collect the validation and fixture observations.

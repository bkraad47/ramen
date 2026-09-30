# numpy_group (fixture)

v0.5.5 I13: proves a group's `mcp/requirements.txt` actually reaches the worker's `pip install`, not just
that the mechanism runs without error against an empty file (every other fixture group has none). One tool,
`numpy_stats_tool`, imports `numpy` and fails at call time if the package isn't really on `sys.path`.

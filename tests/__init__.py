"""Standards-based unit tests for the download engine and its client.

Modules: `support` (local fake CDN), `test_process`, `test_transfers`,
`test_hub_transfer`, `test_engine_recovery`. Run with
`python -m unittest discover -s tests -v`. No test reaches the internet:
`HubClient` is pointed at a 127.0.0.1 server, and `bpy` is never imported.
"""

"""Phase 3 synchronization: wire protocol, device credentials, HTTP
transport, retry/backoff, and the background sync worker.

Only summarized records are synced (work sessions, activity periods, idle
periods, daily app usage). Raw ``activity_events`` never leave the machine.
"""

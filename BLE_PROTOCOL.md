# BLE protocol

The existing adapter handles BLE GATT or serial ELM-compatible requests. Linux uses BlueZ discovery and optional application-managed pairing. A Carista EVO exposes service 18F0 with write characteristic 2AF1 and notification characteristic 2AF0. The application verifies ELM identity during initialization and serializes diagnostic commands. Stop and reconnect on transport errors. The scanner uses standard read requests for VIN, codes, supported PIDs and available freeze-frame data. See README.md for fleet timing and connection behavior.

Phase 1 reads scan records by VIN. Scanner assignments, alternate hardware protocols and battery telemetry remain later phases.

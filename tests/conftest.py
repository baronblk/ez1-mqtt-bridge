"""Shared pytest fixtures for the ez1-mqtt-bridge test suite.

Phase-specific fixtures (mock EZ1 server, embedded MQTT broker, etc.) land
in later phases. Phase 0 keeps this file minimal so the test runner has a
valid root config file.
"""

"""Shared synthetic fixtures for Workstream F regression tests.

Every fixture here is CLEARLY LABELED synthetic, lives in memory only, and is
never written to the genuine decision/request/authorization stores. Tests
that touch store files use tmp_path exclusively.
"""

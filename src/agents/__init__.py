"""Cleaning agents.

Each module here is one agent: prose or a raw value goes in, structured rows come
out. Agents may reason (always through ``CachedLLM``); they never touch the network
directly and never own more than their own file plus their own ``skills/*.md``.
"""

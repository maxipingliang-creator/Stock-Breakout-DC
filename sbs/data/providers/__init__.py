"""Concrete data providers. Each module self-registers via @register_provider.

Importing this package does not import the providers; the base layer imports
them lazily (see ``sbs.data.base._ensure_providers_imported``) so optional
vendor SDKs are only required when actually selected.
"""

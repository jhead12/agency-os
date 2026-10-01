"""
Plugin auto-discovery.

Scans plugins/<type>/ for *.py files (skipping _base.py and __init__.py),
imports each module, and finds the class that implements the corresponding
protocol by looking for a string `key` attribute. The class is instantiated
and registered under its key.

Drop a new .py file in the right folder → it's live. No registration code.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from typing import Any


class PluginRegistry:
    """Discovers and holds all plugin instances by category."""

    def __init__(self) -> None:
        self.sources: dict[str, Any] = {}
        self.products: dict[str, Any] = {}
        self.channels: dict[str, Any] = {}
        self.enrichers: dict[str, Any] = {}

    def discover(self, base_dir: str = "plugins") -> None:
        """Scan all plugin categories and register found plugins."""
        categories = [
            ("prospect_sources", self.sources),
            ("products", self.products),
            ("channels", self.channels),
            ("enrichers", self.enrichers),
        ]

        for category, registry in categories:
            cat_dir = Path(base_dir) / category
            if not cat_dir.exists():
                continue
            for py_file in sorted(cat_dir.glob("*.py")):
                if py_file.name.startswith("_") or py_file.name == "__init__.py":
                    continue
                mod_path = f"plugins.{category}.{py_file.stem}"
                try:
                    mod = importlib.import_module(mod_path)
                except Exception as exc:
                    print(f"  ! Failed to import {mod_path}: {exc}")
                    continue

                # Find the class with a string `key` attribute
                for attr_name in dir(mod):
                    obj = getattr(mod, attr_name)
                    if not inspect.isclass(obj):
                        continue
                    if obj.__module__ != mod_path:
                        continue  # skip imported classes
                    key = getattr(obj, "key", None)
                    if isinstance(key, str) and key:
                        try:
                            instance = obj()
                            registry[key] = instance
                            print(f"  + {category}/{key}")
                        except Exception as exc:
                            print(f"  ! Failed to instantiate {mod_path}.{attr_name}: {exc}")

    def get_source(self, key: str):
        return self.sources.get(key)

    def get_product(self, key: str):
        return self.products.get(key)

    def get_channel(self, key: str):
        return self.channels.get(key)

    def get_enricher(self, key: str):
        return self.enrichers.get(key)

    def list_plugins(self) -> dict[str, list[str]]:
        return {
            "prospect_sources": list(self.sources.keys()),
            "products": list(self.products.keys()),
            "channels": list(self.channels.keys()),
            "enrichers": list(self.enrichers.keys()),
        }
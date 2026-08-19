"""Import agent modules that live in hyphenated folders.

The project layout uses ``agents/ceo-daily-brief/`` and ``agents/amocrm-followup/``.
Hyphens are not valid in Python identifiers, so these cannot be imported with a
normal ``import`` statement. This loader imports them by file path instead,
caching the result so repeated calls are cheap.
"""

from __future__ import annotations

import importlib.util
import sys
from types import ModuleType

from integrations.common.config import PROJECT_ROOT

_CACHE: dict[str, ModuleType] = {}


def load_agent(folder: str, module: str = "agent") -> ModuleType:
    """Import ``agents/<folder>/<module>.py`` as a module object.

    Args:
        folder: Agent folder name, e.g. ``'amocrm-followup'``.
        module: File stem inside that folder, without ``.py``.

    Returns:
        The imported module.

    Raises:
        FileNotFoundError: if the file does not exist.
        ImportError: if the module cannot be loaded or executed.
    """
    key = f"{folder}/{module}"
    if key in _CACHE:
        return _CACHE[key]

    path = PROJECT_ROOT / "agents" / folder / f"{module}.py"
    if not path.exists():
        raise FileNotFoundError(f"Agent module not found: {path}")

    spec = importlib.util.spec_from_file_location(f"mgmg_agent_{folder.replace('-', '_')}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot build an import spec for {path}")

    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)

    _CACHE[key] = loaded
    return loaded

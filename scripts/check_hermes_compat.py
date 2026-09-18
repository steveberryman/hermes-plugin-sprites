#!/usr/bin/env python3
"""Check this plugin against an installed Hermes, without a token or network.

Run it with Hermes' own interpreter, e.g. as a Docker build step:

    cd /opt/hermes && /opt/hermes/.venv/bin/python \\
        /path/to/plugin/scripts/check_hermes_compat.py /path/to/plugin

Exits non-zero if the plugin would fail to load or construct against this
Hermes. ``hermes plugins validate`` is not enough on its own: it imports
``__init__.py`` and calls ``register()``, but ``sprites_environment`` is only
imported lazily from ``create_environment()`` — which is exactly where the
v0.21 ``tools/environments`` refactor broke it.
"""

import importlib
import importlib.metadata
import importlib.util
import inspect
import os
import sys
import types
from pathlib import Path

failures: list[str] = []


def check(ok: bool, what: str) -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        failures.append(what)


def params(fn) -> set[str]:
    return set(inspect.signature(fn).parameters)


def main() -> int:
    plugin_dir = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
    plugin_dir = plugin_dir.resolve()
    hermes_root = os.environ.get("HERMES_ROOT", os.getcwd())
    if hermes_root not in sys.path:
        sys.path.insert(0, hermes_root)

    import yaml
    from packaging.requirements import Requirement

    manifest = yaml.safe_load((plugin_dir / "plugin.yaml").read_text(encoding="utf-8")) or {}
    print(f"Checking plugin {manifest.get('name')!r} at {plugin_dir}")

    # 1. Hermes version gate declared by the plugin.
    try:
        from hermes_cli.plugins_manifest import running_hermes_version, version_satisfies
        current = running_hermes_version()
        spec = str(manifest.get("requires_hermes") or "")
        check(not spec or version_satisfies(spec, current),
              f"requires_hermes {spec or '(none)'} vs running {current}")
    except ImportError as e:
        check(False, f"hermes_cli importable from {hermes_root}: {e}")
        return 1

    # 2. Declared Python dependencies are installed at a satisfying version.
    for req_str in manifest.get("python_dependencies") or manifest.get("pip_dependencies") or []:
        req = Requirement(req_str)
        try:
            have = importlib.metadata.version(req.name)
            check(req.specifier.contains(have, prereleases=True), f"{req.name} {have} satisfies {req_str}")
        except importlib.metadata.PackageNotFoundError:
            check(False, f"{req_str} installed")

    # 3. Load the plugin the way Hermes' directory loader does (a package with
    #    __path__, so relative imports resolve) and run register().
    pkg = "hermes_plugins_compat_check"
    ns = types.ModuleType(pkg)
    ns.__path__ = []
    sys.modules[pkg] = ns
    mod_name = f"{pkg}.{manifest.get('name', 'plugin')}"
    spec = importlib.util.spec_from_file_location(
        mod_name, plugin_dir / "__init__.py", submodule_search_locations=[str(plugin_dir)])
    module = importlib.util.module_from_spec(spec)
    module.__package__ = mod_name
    module.__path__ = [str(plugin_dir)]
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
        check(True, "plugin package imports")
    except Exception as e:
        check(False, f"plugin package imports: {type(e).__name__}: {e}")
        return 1

    providers = []

    class Ctx:
        def register_terminal_environment_provider(self, provider):
            providers.append(provider)

        def __getattr__(self, name):  # other register_* calls are irrelevant here
            return lambda *a, **k: None

    try:
        module.register(Ctx())
        check(len(providers) == 1, "register() registers one terminal environment provider")
    except Exception as e:
        check(False, f"register(): {type(e).__name__}: {e}")
        return 1

    from agent.terminal_env_provider import TerminalEnvironmentProvider
    provider = providers[0]
    check(isinstance(provider, TerminalEnvironmentProvider), "provider is a TerminalEnvironmentProvider")
    check("container_config" in params(provider.create_environment)
          and "kwargs" in params(provider.create_environment),
          "create_environment accepts container_config and **kwargs")

    # 4. The lazily imported backend module, and the Hermes internals it borrows.
    try:
        env_mod = importlib.import_module(f"{mod_name}.sprites_environment")
        check(True, "sprites_environment imports")
    except Exception as e:
        check(False, f"sprites_environment imports: {type(e).__name__}: {e}")
        return 1

    from tools.environments.base import BaseEnvironment
    from tools.environments.base_output import _ThreadedProcessHandle
    from tools.environments.file_sync import FileSyncManager

    env_cls = env_mod.SpritesEnvironment
    check(issubclass(env_cls, BaseEnvironment), "SpritesEnvironment subclasses BaseEnvironment")
    check(not inspect.isabstract(env_cls),
          f"SpritesEnvironment implements all abstract methods "
          f"({', '.join(sorted(getattr(env_cls, '__abstractmethods__', ()))) or 'none missing'})")
    check({"cwd", "timeout"} <= params(BaseEnvironment.__init__), "BaseEnvironment.__init__(cwd, timeout)")
    check(params(BaseEnvironment._run_bash) <= params(env_cls._run_bash),
          f"_run_bash override accepts base params {sorted(params(BaseEnvironment._run_bash))}")
    for hook in ("_before_execute", "init_session", "execute"):
        check(callable(getattr(BaseEnvironment, hook, None)), f"BaseEnvironment.{hook} exists")
    check({"exec_fn", "cancel_fn"} <= params(_ThreadedProcessHandle.__init__),
          "_ThreadedProcessHandle(exec_fn, cancel_fn)")
    check({"get_files_fn", "upload_fn", "delete_fn"} <= params(FileSyncManager.__init__),
          "FileSyncManager(get_files_fn, upload_fn, delete_fn)")

    # 5. The SDK surface the backend uses.
    try:
        from sprites import SpritesClient  # noqa: F401
        from sprites.exceptions import ExitError, NotFoundError, SpriteError, TimeoutError  # noqa: F401
        check(True, "sprites SDK exposes SpritesClient and the exceptions used")
    except ImportError as e:
        check(False, f"sprites SDK surface: {e}")

    print("PASS" if not failures else f"FAIL ({len(failures)} check(s))")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

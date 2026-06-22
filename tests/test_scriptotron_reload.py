import os
import importlib
import sys
import threading
import textwrap
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
ENSO_PACKAGE_ROOT = REPO_ROOT / "enso"
if str(ENSO_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENSO_PACKAGE_ROOT))

from enso import config


class FakeEventManager(object):
    def __init__(self):
        self.responders = {}

    def registerResponder(self, func, eventName):
        self.responders.setdefault(eventName, []).append(func)


class FakeCommandManager(object):
    def __init__(self, fail_on=None):
        self.commands = {}
        self.fail_on = set(fail_on or [])

    def registerCommand(self, cmdName, cmdObj):
        if cmdName in self.fail_on:
            raise RuntimeError("forced registration failure: %s" % cmdName)
        if cmdName in self.commands:
            raise RuntimeError("duplicate registration: %s" % cmdName)
        self.commands[cmdName] = cmdObj

    def unregisterCommand(self, cmdName):
        if cmdName not in self.commands:
            raise RuntimeError("missing command: %s" % cmdName)
        del self.commands[cmdName]

    def getCommands(self):
        return dict(self.commands)


def write_file(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")


@contextmanager
def isolated_state_module(root_dir):
    root_dir = Path(root_dir)
    root_dir.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(root_dir))
    sys.modules.pop("state", None)
    try:
        yield
    finally:
        sys.modules.pop("state", None)
        if str(root_dir) in sys.path:
            sys.path.remove(str(root_dir))


def load_tracker_module():
    package_name = "enso.contrib.scriptotron"
    module_name = "enso.contrib.scriptotron.tracker"
    package_dir = ENSO_PACKAGE_ROOT / "enso" / "contrib" / "scriptotron"

    sys.modules.pop(module_name, None)

    selection_stub = sys.modules.get("enso.selection")
    if selection_stub is None:
        selection_stub = ModuleType("enso.selection")
        selection_stub.set = lambda *args, **kwargs: None
        sys.modules["enso.selection"] = selection_stub

    messages_stub = sys.modules.get("enso.messages")
    if messages_stub is None:
        messages_stub = ModuleType("enso.messages")
        messages_stub.displayMessage = lambda *args, **kwargs: None
        sys.modules["enso.messages"] = messages_stub

    package = ModuleType(package_name)
    package.__path__ = [str(package_dir)]
    sys.modules[package_name] = package

    return importlib.import_module(module_name)


def build_tracker(script_dir, user_dir, command_manager=None):
    Path(script_dir).mkdir(parents=True, exist_ok=True)
    Path(user_dir).mkdir(parents=True, exist_ok=True)
    Path(user_dir).joinpath("commands").mkdir(parents=True, exist_ok=True)

    event_manager = FakeEventManager()
    command_manager = command_manager or FakeCommandManager()
    tracker_module = load_tracker_module()

    with mock.patch("enso.providers.getInterface", side_effect=lambda name: (lambda: str(script_dir))):
        with mock.patch.object(config, "ENSO_USER_DIR", str(user_dir), create=True):
            with mock.patch.object(config, "TRACK_COMMAND_CHANGES", False, create=True):
                tracker = tracker_module.ScriptTracker(event_manager, command_manager)
    return tracker, event_manager, command_manager, tracker_module


class ScriptotronReloadTests(unittest.TestCase):
    def test_path_normalization_matches_windows_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            target = tmp_path / "Nested" / "sample.py"
            write_file(target, "print('x')\n")
            tracker_module = load_tracker_module()

            alias_a = str(target)
            alias_b = str(target.parent / ".." / target.parent.name / target.name).replace("Nested", "nEsTeD")

            self.assertEqual(
                tracker_module.normalizePath(alias_a),
                tracker_module.normalizePath(alias_b),
            )

    def test_pending_changes_are_thread_safe(self):
        tracker_module = load_tracker_module()
        tracker = tracker_module.ScriptTracker.__new__(tracker_module.ScriptTracker)
        tracker._pendingLock = threading.Lock()
        tracker._pendingFiles = set()
        tracker._fullScanPending = False

        with tempfile.TemporaryDirectory() as tmp:
            a = str(Path(tmp) / "a.py")
            b = str(Path(tmp) / "b.py")

            threads = [
                threading.Thread(target=tracker.setPendingChanges, args=(a,)),
                threading.Thread(target=tracker.setPendingChanges, args=(b,)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            files, full_scan = tracker._drainPendingChanges()

        self.assertFalse(full_scan)
        self.assertEqual(
            files,
            {tracker_module.normalizePath(a), tracker_module.normalizePath(b)},
        )

    def test_modifying_a_does_not_recreate_b(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script_dir = root / "scripts"
            user_dir = root / "user"
            commands_dir = user_dir / "commands"
            write_file(root / "state.py", "events = []\n")

            write_file(commands_dir / "a.py", """
                def cmd_a(ensoapi):
                    return "A1"
            """)
            write_file(commands_dir / "b.py", """
                def cmd_b(ensoapi):
                    return "B1"
            """)

            with isolated_state_module(root):
                tracker, _, command_manager, tracker_module = build_tracker(script_dir, user_dir)
                b_before = command_manager.getCommands()["b"]

                write_file(commands_dir / "a.py", """
                    def cmd_a(ensoapi):
                        return "A2"
                """)
                tracker.setPendingChanges(str(commands_dir / "a.py"))
                tracker._updateScripts()

                self.assertIs(command_manager.getCommands()["b"], b_before)

    def test_syntax_error_in_a_keeps_previous_version_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script_dir = root / "scripts"
            user_dir = root / "user"
            commands_dir = user_dir / "commands"
            write_file(root / "state.py", "events = []\n")

            write_file(commands_dir / "a.py", """
                def cmd_a(ensoapi):
                    return "A1"
            """)

            with isolated_state_module(root):
                tracker, _, command_manager, tracker_module = build_tracker(script_dir, user_dir)
                a_before = command_manager.getCommands()["a"]

                write_file(commands_dir / "a.py", """
                    def cmd_a(ensoapi)
                        return "BROKEN"
                """)
                tracker.setPendingChanges(str(commands_dir / "a.py"))
                tracker._updateScripts()

                self.assertIs(command_manager.getCommands()["a"], a_before)
                self.assertEqual(
                    tracker._loadedScripts[
                        tracker_module.normalizePath(str(commands_dir / "a.py"))
                    ].status,
                    "loaded",
                )

    def test_deleting_a_does_not_affect_b(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script_dir = root / "scripts"
            user_dir = root / "user"
            commands_dir = user_dir / "commands"
            write_file(root / "state.py", "events = []\n")

            write_file(commands_dir / "a.py", """
                def cmd_a(ensoapi):
                    return "A1"
            """)
            write_file(commands_dir / "b.py", """
                def cmd_b(ensoapi):
                    return "B1"
            """)

            with isolated_state_module(root):
                tracker, _, command_manager, tracker_module = build_tracker(script_dir, user_dir)
                b_before = command_manager.getCommands()["b"]

                os.remove(commands_dir / "a.py")
                tracker.setPendingChanges(str(commands_dir / "a.py"))
                tracker._updateScripts()

                self.assertNotIn("a", command_manager.getCommands())
                self.assertIs(command_manager.getCommands()["b"], b_before)

    def test_duplicate_name_rolls_back_complete_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script_dir = root / "scripts"
            user_dir = root / "user"
            commands_dir = user_dir / "commands"
            write_file(root / "state.py", "events = []\n")

            write_file(commands_dir / "a.py", """
                def cmd_a(ensoapi):
                    return "A1"
            """)
            write_file(commands_dir / "b.py", """
                def cmd_b(ensoapi):
                    return "B1"
            """)

            with isolated_state_module(root):
                tracker, _, command_manager, tracker_module = build_tracker(script_dir, user_dir)
                a_before = command_manager.getCommands()["a"]
                b_before = command_manager.getCommands()["b"]

                write_file(commands_dir / "a.py", """
                    def cmd_b(ensoapi):
                        return "A2"
                """)
                tracker.setPendingChanges(str(commands_dir / "a.py"))
                tracker._updateScripts()

                self.assertIs(command_manager.getCommands()["a"], a_before)
                self.assertIs(command_manager.getCommands()["b"], b_before)

    def test_no_handler_ghost_after_failed_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script_dir = root / "scripts"
            user_dir = root / "user"
            commands_dir = user_dir / "commands"
            write_file(root / "state.py", "events = []\n")

            write_file(commands_dir / "a.py", """
                from state import events

                def cmd_a(ensoapi):
                    return "A1"

                def start():
                    events.append("old")

                cmd_a.on_quasimode_start = start
            """)
            write_file(commands_dir / "b.py", """
                def cmd_b(ensoapi):
                    return "B1"
            """)

            with isolated_state_module(root):
                import state

                tracker, _, _, tracker_module = build_tracker(script_dir, user_dir)
                tracker._onQuasimodeStart()
                self.assertEqual(state.events, ["old"])

                state.events[:] = []
                write_file(commands_dir / "a.py", """
                    from state import events

                    def cmd_b(ensoapi):
                        return "A2"

                    def start():
                        events.append("new")

                    cmd_b.on_quasimode_start = start
                """)
                tracker.setPendingChanges(str(commands_dir / "a.py"))
                tracker._updateScripts()
                tracker._onQuasimodeStart()

                self.assertEqual(state.events, ["old"])


if __name__ == "__main__":
    unittest.main()

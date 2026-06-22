import logging
import os
import threading
import tokenize
import traceback
import types

from enso import config
from enso.commands.manager import CommandAlreadyRegisteredError
from enso.contrib.scriptotron import adapters
from enso.contrib.scriptotron import cmdretriever
from enso.contrib.scriptotron import concurrency
from enso.contrib.scriptotron import ensoapi
from enso.contrib.scriptotron.tracebacks import TracebackCommand
from enso.contrib.scriptotron.tracebacks import safetyNetted


def normalizePath(path):
    return os.path.normcase(
        os.path.realpath(
            os.path.abspath(path)
        )
    )


def _getFileSignature(path):
    stat = os.stat(path)
    mtime_ns = getattr(stat, "st_mtime_ns", None)
    if mtime_ns is None:
        mtime_ns = int(stat.st_mtime * 1000000000)
    return (mtime_ns, stat.st_size)


class ScriptConflictError(Exception):
    pass


class LoadedScript(object):
    def __init__(self, path):
        self.path = normalizePath(path)
        self.commands = []
        self.commandExprs = []
        self.handlers = []
        self.dependencies = []
        self.fingerprint = None
        self.globals = {}
        self.status = "loaded"
        self.lastError = None
        self.failedFingerprint = None


class ScriptTracker(object):
    def __init__(self, eventManager, commandManager):
        from enso.providers import getInterface

        self._cmdMgr = commandManager
        self._genMgr = concurrency.GeneratorManager(eventManager)
        self._scriptFolder = getInterface("scripts_folder")()
        self._loadedScripts = {}
        self._dependentsByFile = {}
        self._lastSignatures = {}
        self._failedScripts = {}
        self._pendingLock = threading.Lock()
        self._pendingFiles = set()
        self._fullScanPending = False

        eventManager.registerResponder(self._updateScripts, "startQuasimode")
        eventManager.registerResponder(self._onQuasimodeStart, "startQuasimode")

        commandManager.registerCommand(TracebackCommand.NAME, TracebackCommand())
        self._updateScripts(True)

    @classmethod
    def install(cls, eventManager, commandManager):
        cls._instance = cls(eventManager, commandManager)

    @classmethod
    def get(cls):
        return cls._instance

    def setPendingChanges(self, fileName=None):
        with self._pendingLock:
            if fileName is None:
                self._fullScanPending = True
            else:
                self._pendingFiles.add(normalizePath(fileName))

    def _drainPendingChanges(self):
        with self._pendingLock:
            files = set(self._pendingFiles)
            fullScan = self._fullScanPending
            self._pendingFiles.clear()
            self._fullScanPending = False
        return files, fullScan

    def clearCommands(self):
        for script in list(self._loadedScripts.values()):
            self._detachScriptCommands(script)
        self._loadedScripts = {}
        self._dependentsByFile = {}
        self._lastSignatures = {}
        self._failedScripts = {}
        self._genMgr.reset()

    @safetyNetted
    def _callHandler(self, handler, owner):
        result = handler()
        if isinstance(result, types.GeneratorType):
            self._genMgr.add(result, owner)

    def _onQuasimodeStart(self):
        for scriptPath in sorted(self._loadedScripts.keys(), key=str.lower):
            script = self._loadedScripts[scriptPath]
            for handler in script.handlers:
                self._callHandler(handler, script.path)

    @safetyNetted
    def _getGlobalsFromSourceCode(self, text, filename):
        allGlobals = {}
        code = compile(text, filename, "exec")
        try:
            exec(code, allGlobals)
        except Exception:
            logging.exception("Failed executing %s", filename)
            raise
        return allGlobals

    def _getCommandFiles(self):
        commandFiles = []
        folders = [
            self._scriptFolder,
            os.path.join(config.ENSO_USER_DIR, "commands"),
        ]

        for folder in folders:
            try:
                fileNames = sorted(os.listdir(folder), key=str.lower)
            except OSError as error:
                logging.warning(
                    "Unable to enumerate Scriptotron folder %s: %s",
                    folder,
                    error,
                )
                continue

            for fileName in fileNames:
                if fileName.endswith(".py"):
                    commandFiles.append(
                        normalizePath(os.path.join(folder, fileName))
                    )

        return commandFiles

    def _collectDependencies(self, path, allGlobals):
        dependencies = set([normalizePath(path)])

        for obj in list(allGlobals.values()):
            code = getattr(obj, "__code__", None)
            if code is None:
                continue
            if getattr(obj, "__module__", None) is None:
                dependencies.add(normalizePath(code.co_filename))

        return sorted(dependencies, key=str.lower)

    def _prepareScript(self, fileName):
        path = normalizePath(fileName)

        with tokenize.open(path) as sourceFile:
            text = sourceFile.read()

        allGlobals = self._getGlobalsFromSourceCode(text, path)
        candidate = LoadedScript(path)
        candidate.globals = allGlobals
        candidate.fingerprint = _getFileSignature(path)

        category = os.path.splitext(os.path.basename(path))[0].replace("_", " ")
        if "CATEGORY" in allGlobals:
            category = allGlobals["CATEGORY"]

        for name, value in allGlobals.items():
            if callable(value) and name.startswith(cmdretriever.SCRIPT_PREFIX):
                value.category = category
                value.cmdFile = path

        infos = cmdretriever.getCommandsFromObjects(allGlobals)
        for info in infos:
            command = adapters.makeCommandFromInfo(
                info,
                ensoapi.EnsoApi(),
                self._genMgr,
                commandFile=path,
            )
            candidate.commands.append((info["cmdExpr"], command))
            if hasattr(info["func"], "on_quasimode_start"):
                candidate.handlers.append(info["func"].on_quasimode_start)

        candidate.commandExprs = [expr for expr, _ in candidate.commands]
        candidate.dependencies = self._collectDependencies(path, allGlobals)
        return candidate

    def _validateCandidate(self, candidate, oldScript):
        existing = self._cmdMgr.getCommands()
        ownedByOldScript = set(oldScript.commandExprs if oldScript else [])
        seen = set()

        for commandExpr in candidate.commandExprs:
            if commandExpr in seen:
                raise ScriptConflictError(
                    "Command declared twice in %s: %s"
                    % (candidate.path, commandExpr)
                )

            if commandExpr in existing and commandExpr not in ownedByOldScript:
                raise ScriptConflictError(
                    "Command '%s' is already provided by another script."
                    % commandExpr
                )

            seen.add(commandExpr)

    def _detachScriptCommands(self, script):
        if script is None:
            return

        for commandExpr in reversed(script.commandExprs):
            try:
                self._cmdMgr.unregisterCommand(commandExpr)
            except Exception as error:
                logging.warning(
                    "Unable to unregister %s from %s: %s",
                    commandExpr,
                    script.path,
                    error,
                )

        self._genMgr.reset(script.path)

    def _attachScriptCommands(self, script):
        registered = []

        try:
            for commandExpr, command in script.commands:
                self._cmdMgr.registerCommand(commandExpr, command)
                registered.append(commandExpr)
        except Exception:
            for commandExpr in reversed(registered):
                try:
                    self._cmdMgr.unregisterCommand(commandExpr)
                except Exception:
                    logging.exception(
                        "Rollback failed while detaching %s from %s",
                        commandExpr,
                        script.path,
                    )
            raise

        script.commandExprs = registered

    def _removeDependencyIndexForScript(self, script):
        for dependency in script.dependencies:
            dependents = self._dependentsByFile.get(dependency)
            if not dependents:
                continue
            dependents.discard(script.path)
            if not dependents:
                del self._dependentsByFile[dependency]

    def _addDependencyIndexForScript(self, script):
        for dependency in script.dependencies:
            self._dependentsByFile.setdefault(dependency, set()).add(script.path)

    def _recordLoadedScript(self, script):
        self._loadedScripts[script.path] = script
        self._addDependencyIndexForScript(script)
        self._lastSignatures[script.path] = script.fingerprint
        for dependency in script.dependencies:
            if os.path.exists(dependency):
                self._lastSignatures[dependency] = _getFileSignature(dependency)
        self._failedScripts.pop(script.path, None)

    def _recordFailure(self, fileName, error, candidate=None, oldScript=None):
        path = normalizePath(fileName)
        failedScript = candidate or LoadedScript(path)
        failedScript.status = "error"
        failedScript.lastError = traceback.format_exc()
        if candidate is not None:
            failedScript.failedFingerprint = candidate.fingerprint
        self._failedScripts[path] = failedScript

        if oldScript is not None:
            logging.warning(
                "Could not reload %s. The previous working version is still active.",
                path,
            )
        else:
            logging.warning("Could not load %s.", path)

        logging.debug("Reload failure for %s: %s", path, error, exc_info=True)

    def _reloadScript(self, fileName):
        path = normalizePath(fileName)
        oldScript = self._loadedScripts.get(path)
        currentSignature = None
        if os.path.exists(path):
            currentSignature = _getFileSignature(path)

        failedScript = self._failedScripts.get(path)
        if failedScript is not None and failedScript.failedFingerprint == currentSignature:
            logging.debug("Skipping unchanged failed version of %s", path)
            return False

        oldDetached = False
        candidate = None

        try:
            candidate = self._prepareScript(path)
            self._validateCandidate(candidate, oldScript)

            if oldScript is not None:
                self._detachScriptCommands(oldScript)
                oldDetached = True

            self._attachScriptCommands(candidate)
        except Exception as error:
            if oldDetached and oldScript is not None:
                try:
                    self._attachScriptCommands(oldScript)
                except Exception:
                    logging.exception(
                        "Unable to restore the previous version of %s after a failed reload.",
                        path,
                    )
                    raise

            self._recordFailure(path, error, candidate, oldScript)
            return False

        if oldScript is not None:
            self._removeDependencyIndexForScript(oldScript)

        self._recordLoadedScript(candidate)
        candidate.status = "loaded"
        candidate.lastError = None
        candidate.failedFingerprint = None
        logging.info("Reloaded %s", path)
        return True

    def _unloadScript(self, fileName):
        path = normalizePath(fileName)
        oldScript = self._loadedScripts.pop(path, None)
        if oldScript is None:
            return False

        self._detachScriptCommands(oldScript)
        self._removeDependencyIndexForScript(oldScript)
        self._lastSignatures.pop(path, None)
        self._failedScripts.pop(path, None)
        logging.info("Unloaded %s", path)
        return True

    def _expandAffectedScripts(self, changedFiles):
        affected = set(changedFiles)
        pending = list(changedFiles)

        while pending:
            current = pending.pop()
            for dependent in self._dependentsByFile.get(current, ()):
                if dependent not in affected:
                    affected.add(dependent)
                    pending.append(dependent)

        return affected

    def _updateScripts(self, init=False):
        pendingFiles, fullScanPending = self._drainPendingChanges()
        currentFiles = set(self._getCommandFiles())
        changedFiles = set(pendingFiles)

        if init or config.TRACK_COMMAND_CHANGES or fullScanPending:
            watchedFiles = set(self._lastSignatures.keys()) | currentFiles

            for fileName in watchedFiles:
                if os.path.exists(fileName):
                    signature = _getFileSignature(fileName)
                    if signature != self._lastSignatures.get(fileName):
                        changedFiles.add(fileName)
                elif fileName in self._lastSignatures:
                    changedFiles.add(fileName)

            for fileName in currentFiles:
                if fileName not in self._lastSignatures:
                    changedFiles.add(fileName)

            for fileName in list(self._loadedScripts.keys()):
                if fileName not in currentFiles:
                    changedFiles.add(fileName)

        affectedScripts = self._expandAffectedScripts(changedFiles)

        for fileName in sorted(affectedScripts, key=str.lower):
            if os.path.exists(fileName):
                self._reloadScript(fileName)
            else:
                self._unloadScript(fileName)


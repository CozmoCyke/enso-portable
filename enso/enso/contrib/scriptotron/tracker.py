import logging
import os
import types

import traceback

from enso import config
from enso.commands.manager import CommandAlreadyRegisteredError
from enso.contrib.scriptotron.tracebacks import TracebackCommand
from enso.contrib.scriptotron.tracebacks import safetyNetted
from enso.contrib.scriptotron.events import EventResponderList
from enso.contrib.scriptotron import adapters
from enso.contrib.scriptotron import cmdretriever
from enso.contrib.scriptotron import ensoapi
from enso.contrib.scriptotron import concurrency


class ScriptCommandTracker:
    def __init__( self, commandManager, eventManager ):
        self._cmdExprs = []
        self._cmdExprsByFile = {}
        self._qmStartEventsByFile = {}
        self._cmdMgr = commandManager
        self._genMgr = concurrency.GeneratorManager( eventManager )
        self._qmStartEvents = EventResponderList(
            eventManager,
            "startQuasimode",
            self._onQuasimodeStart
            )

    @safetyNetted
    def _callHandler( self, handler ):
        result = handler()
        if isinstance( result, types.GeneratorType ):
            self._genMgr.add( result )

    def _onQuasimodeStart( self ):
        for handler in self._qmStartEvents:
            self._callHandler( handler )

    def _removeQuasimodeHandlers( self, handlers ):
        handlers = list( handlers )
        remainingHandlers = []

        for currentHandler in self._qmStartEvents:
            for index, handler in enumerate( handlers ):
                if currentHandler is handler:
                    del handlers[index]
                    break
            else:
                remainingHandlers.append( currentHandler )

        self._qmStartEvents[:] = remainingHandlers

    def clearCommands( self, commandFile = None ):
        if commandFile is None:
            cmdExprs = list( self._cmdExprs )
            self._cmdExprsByFile = {}
            self._qmStartEventsByFile = {}
            self._qmStartEvents[:] = []
        else:
            cmdExprs = self._cmdExprsByFile.pop( commandFile, [] )
            handlers = self._qmStartEventsByFile.pop( commandFile, [] )
            self._removeQuasimodeHandlers( handlers )

        for cmdExpr in cmdExprs:
            self._cmdMgr.unregisterCommand( cmdExpr )

        if commandFile is None:
            self._cmdExprs = []
        else:
            self._cmdExprs = [
                cmdExpr for cmdExpr in self._cmdExprs
                if cmdExpr not in cmdExprs
                ]

        # A generator may still reference globals from the previous version of
        # a script.  Resetting generators is cheap and avoids running stale
        # code while leaving commands from unchanged files registered.
        self._genMgr.reset()

    def _registerCommand( self, cmdObj, cmdExpr, commandFile = None ):
        try:
            self._cmdMgr.registerCommand( cmdExpr, cmdObj )
            self._cmdExprs.append( cmdExpr )
            if commandFile is not None:
                self._cmdExprsByFile.setdefault( commandFile, [] ).append(
                    cmdExpr
                    )
        except CommandAlreadyRegisteredError:
            logging.warning( "Command already registered: %s" % cmdExpr )

    def registerNewCommands( self, commandInfoList, commandFile = None ):
        if commandFile is not None:
            self._cmdExprsByFile.setdefault( commandFile, [] )
            self._qmStartEventsByFile.setdefault( commandFile, [] )

        for info in commandInfoList:
            if hasattr( info["func"], "on_quasimode_start" ):
                handler = info["func"].on_quasimode_start
                self._qmStartEvents.append( handler )
                if commandFile is not None:
                    self._qmStartEventsByFile[commandFile].append( handler )
            cmd = adapters.makeCommandFromInfo(
                info,
                ensoapi.EnsoApi(),
                self._genMgr
                )
            self._registerCommand( cmd, info["cmdExpr"], commandFile )


class ScriptTracker:
    def __init__( self, eventManager, commandManager ):
        self._scriptCmdTracker = ScriptCommandTracker( commandManager,
                                                       eventManager )
        from enso.providers import getInterface
        self._scriptFolder = getInterface("scripts_folder")()
        self._lastMods = {}
        self._scriptDependencies = {}
        self._fileDependencies = []
        self._registerDependencies()
        self._pendingChanges = False
        self._pendingFiles = set()

        eventManager.registerResponder(
            self._updateScripts,
            "startQuasimode"
            )

        commandManager.registerCommand( TracebackCommand.NAME,
                                        TracebackCommand() )
        self._updateScripts(True)

    @classmethod
    def install( cls, eventManager, commandManager ):
        cls._instance = cls( eventManager, commandManager )

    @classmethod
    def get( cls ):
        return cls._instance

    def setPendingChanges( self, fileName = None ):
        self._pendingChanges = True
        if fileName is not None:
            self._pendingFiles.add( os.path.abspath( fileName ) )

    @safetyNetted
    def _getGlobalsFromSourceCode( self, text, filename ):
        allGlobals = {}
        code = compile( text, filename, "exec" )
        try:
            exec(code, allGlobals)
        except Exception as e:
            print(traceback.format_exc())
            raise e

        return allGlobals

    def _getCommandFiles( self ):
        commandFiles = []
        try:
            commandFiles = [
              os.path.abspath( os.path.join(self._scriptFolder, x) )
              for x in os.listdir(self._scriptFolder)
              if x.endswith(".py")
            ]
        except Exception:
            pass

        try:
            userScriptFolder = os.path.join(config.ENSO_USER_DIR, "commands")
            commandFiles = commandFiles + [
                os.path.abspath( os.path.join(userScriptFolder, x) )
                for x in os.listdir(userScriptFolder)
                if x.endswith(".py")
            ]
        except Exception:
            pass

        # Keep the filesystem order while avoiding loading the same absolute
        # path twice when both script providers point at one folder.
        uniqueFiles = []
        seenFiles = set()
        for fileName in commandFiles:
            if fileName not in seenFiles:
                uniqueFiles.append( fileName )
                seenFiles.add( fileName )

        return uniqueFiles

    def _reloadPyScripts( self, commandFiles = None ):
        if commandFiles is None:
            commandFiles = self._getCommandFiles()

        print(commandFiles)

        for f in commandFiles:
            if not os.path.exists( f ):
                self._scriptCmdTracker.clearCommands( f )
                self._scriptDependencies.pop( f, None )
                continue

            try:
                text = open( f, "r" ).read()
            except Exception:
                continue

            allGlobals = self._getGlobalsFromSourceCode(text, f)

            # Keep the last working commands registered when the edited file
            # contains a temporary syntax/runtime error.
            if allGlobals is None:
                continue

            category = os.path.splitext(os.path.basename(f))[0].replace("_", " ")

            if "CATEGORY" in allGlobals:
                category = allGlobals["CATEGORY"]

            for fn in allGlobals:
                if callable(allGlobals[fn]) \
                        and fn.startswith(cmdretriever.SCRIPT_PREFIX):
                    allGlobals[fn].category = category
                    allGlobals[fn].cmdFile = f

            infos = cmdretriever.getCommandsFromObjects( allGlobals )

            # Replace only commands and quasimode handlers owned by this file.
            self._scriptCmdTracker.clearCommands( f )
            self._scriptCmdTracker.registerNewCommands( infos, f )
            self._registerDependencies( allGlobals, f )

        self._refreshFileDependencies()

    def _getExtraDependencies( self, allGlobals, commandFile ):
        extraDeps = []

        for obj in list(allGlobals.values()):
            code = getattr( obj, "__code__", None )
            if code is None:
                code = getattr( obj, "func_code", None )

            if code is not None and getattr(obj, "__module__", None) is None:
                fileName = os.path.abspath( code.co_filename )
                if fileName != commandFile:
                    extraDeps.append( fileName )

        return extraDeps

    def _registerDependencies( self, allGlobals = None, commandFile = None ):
        baseDeps = self._getCommandFiles()

        if commandFile is not None and allGlobals is not None:
            commandFile = os.path.abspath( commandFile )
            extraDeps = self._getExtraDependencies( allGlobals, commandFile )
            self._scriptDependencies[commandFile] = set(
                [commandFile] + extraDeps
                )
        else:
            for fileName in baseDeps:
                self._scriptDependencies.setdefault( fileName, set([fileName]) )

        self._refreshFileDependencies()

    def _refreshFileDependencies( self ):
        dependencies = set( self._getCommandFiles() )
        for scriptDependencies in self._scriptDependencies.values():
            dependencies.update( scriptDependencies )
        self._fileDependencies = list( dependencies )

    def _getModificationTime( self, fileName ):
        try:
            return os.stat( fileName ).st_mtime
        except OSError:
            return None

    def _getChangedFiles( self, commandFiles ):
        dependencies = set( self._fileDependencies )
        dependencies.update( commandFiles )
        changedFiles = set()
        notSeen = object()

        for fileName in dependencies:
            lastMod = self._getModificationTime( fileName )
            if lastMod != self._lastMods.get(fileName, notSeen):
                changedFiles.add( fileName )

        return changedFiles

    def _getAffectedScripts( self, changedFiles, commandFiles ):
        commandFiles = set( commandFiles )
        knownScripts = set( self._scriptDependencies )
        affectedScripts = set()

        # New and deleted command files need loading or unregistering even if
        # they have no dependency record yet.
        affectedScripts.update( commandFiles - knownScripts )
        affectedScripts.update( knownScripts - commandFiles )

        for fileName in changedFiles:
            if fileName in commandFiles or fileName in knownScripts:
                affectedScripts.add( fileName )

            for scriptFile, dependencies in self._scriptDependencies.items():
                if fileName in dependencies:
                    affectedScripts.add( scriptFile )

        return affectedScripts

    def _rememberModificationTimes( self ):
        self._refreshFileDependencies()
        self._lastMods = {
            fileName: self._getModificationTime( fileName )
            for fileName in self._fileDependencies
            }

    def _updateScripts( self, init=False):
        commandFiles = self._getCommandFiles()
        scriptsToReload = []
        checkedForChanges = False

        if init:
            scriptsToReload = commandFiles
            checkedForChanges = True
        elif config.TRACK_COMMAND_CHANGES or self._pendingChanges:
            checkedForChanges = True
            changedFiles = self._getChangedFiles( commandFiles )
            changedFiles.update( self._pendingFiles )
            affectedScripts = self._getAffectedScripts(
                changedFiles,
                commandFiles
                )

            # Preserve command-file order so duplicate command handling remains
            # deterministic.  Deleted files are appended for cleanup.
            scriptsToReload = [
                fileName for fileName in commandFiles
                if fileName in affectedScripts
                ]
            scriptsToReload.extend(
                fileName for fileName in affectedScripts
                if fileName not in commandFiles
                )

        self._pendingChanges = False
        self._pendingFiles.clear()

        if scriptsToReload:
            self._reloadPyScripts( scriptsToReload )

        if checkedForChanges:
            self._rememberModificationTimes()

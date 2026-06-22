import threading

from enso.contrib.scriptotron.tracebacks import safetyNetted
from enso.contrib.scriptotron.events import EventResponderList

class GeneratorManager( object ):
    """
    Responsible for managing generators in a way similar to tasklets
    in Stackless Python by iterating the state of all registered
    generators on every timer tick.
    """

    def __init__( self, eventManager ):
        self.__generators = EventResponderList(
            eventManager,
            "timer",
            self.__onTimer
            )
        self.__lock = threading.Lock()

    @safetyNetted
    def __callGenerator( self, owner, generator, keepAlives ):
        try:
            next(generator)
            keepAlives.append( (owner, generator) )
        except StopIteration:
            pass

    def __onTimer( self, msPassed ):
        with self.__lock:
            generators = list(self.__generators)

        keepAlives = []
        for owner, generator in generators:
            self.__callGenerator( owner, generator, keepAlives )

        with self.__lock:
            self.__generators[:] = keepAlives

    def reset( self, owner = None ):
        with self.__lock:
            if owner is None:
                self.__generators[:] = []
            else:
                self.__generators[:] = [
                    item for item in self.__generators
                    if item[0] != owner
                ]

    def add( self, generator, owner = None ):
        with self.__lock:
            self.__generators.append( (owner, generator) )

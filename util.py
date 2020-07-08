import json
import socket
import threading
import time

import orjson

# port for PyObjSync
import typing

SYNC_PORT = 5001


def current_sync_host():
    return f"{socket.gethostname()}:{SYNC_PORT}"


def indent(s: str, level: int=2):
    """
    Indents each line of a string
    :param s: string to indent
    :param level: amount of spaces to add
    :return: indented string
    """
    return "\n".join([" "*level + line for line in s.split("\n")])


def obj_dumper(obj: any):
    """
    Attempts to pretty print an object
    :param obj: to pretty return
    :return: pretty string repr of obj
    """
    def default(o):
        if callable(o):
            return str(0)

    return indent(
        json.dumps(orjson.loads(orjson.dumps(
            {a: getattr(obj, a) for a in dir(obj)},
            default=default,
        )), indent=2),
        level=4,
    )


class ExpiringSet:
    def __init__(self, *, ttl: int = 30):
        """
        A set with values that expire if they haven't been re-added
        in a given amount of time
        :param ttl: seconds for an item to live
        """
        self._lock = threading.Lock()
        self._data = {}
        self.ttl = ttl

    def add(self, elem: any) -> None:
        """Add an element to the set"""
        with self._lock:
            self._data[elem] = time.time() + self.ttl

    def update(self, *others: typing.Iterable) -> None:
        """
        Updates set with items in iterables
        :param others:
        :return:
        """
        with self._lock:
            for o in others:
                for elem in o:
                    self.add(elem)

    def remove(self, elem: typing.Any) -> typing.Any:
        """
        Remove `elem` from the set if it's present
        :param elem: element to remove
        :return: elem
        :raises:
            KeyError: if elem isn't present in the set
        """

        with self._lock:
            if elem not in self._data:
                e = KeyError("Element not found in set")
                e.context["element"] = elem
                raise e

            del self._data[elem]
            return elem

    def discard(self, elem: typing.Any) -> None:
        """Remove `elem` from the set if it's present"""

        with self._lock:
            try:
                self.remove(elem)
            except KeyError:
                pass

    def pop(self) -> typing.Any:
        """Remove and return an element from the set"""
        with self._lock:
            while True:
                try:
                    elem, exp = next(iter(self._data.items()))
                except StopIteration:
                    raise KeyError("set is empty")
                del self._data[elem]
                if exp >= time.time():
                    return elem

    def clear(self) -> None:
        """Remove all elements from the set"""
        with self._lock:
            self._data = {}

    def __contains__(self, elem: typing.Any) -> bool:
        """Checks whether `elem` is in the set"""
        with self._lock:
            return elem in self._data and self._data[elem] >= time.time()

    def __iter__(self) -> frozenset:
        with self._lock:
            return iter(frozenset([e for e, exp in self._data.items() if exp >= time.time()]))

    def __str__(self) -> str:
        return str(tuple(self.__iter__()))

    def __repr__(self) -> str:
        with self._lock:
            return str(self._data)

    def __getattr__(self, item):
        print(f"Received __getattr__ with {item}")

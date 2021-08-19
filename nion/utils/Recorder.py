import abc
import collections
import copy
import typing
import weakref

from . import Observable

from .ReferenceCounting import weak_partial


AccessorType = typing.Callable[[typing.Any], typing.Any]


class Accessor(abc.ABC):
    @abc.abstractmethod
    def get(self, x) -> typing.Any: ...


class DirectAccessor(Accessor):
    def get(self, o: Observable.Observable) -> typing.Any:
        return o


class KeyAccessor(Accessor):
    def __init__(self, accessor: Accessor, key: str):
        self.accessor = accessor
        self.key = key

    def get(self, o: Observable.Observable) -> typing.Any:
        return getattr(self.accessor.get(o), self.key)


class IndexAccessor(Accessor):
    def __init__(self, accessor: Accessor, index: int):
        self.accessor = accessor
        self.index = index

    def get(self, o: Observable.Observable) -> typing.Any:
        return self.accessor.get(o)[self.index]


class RecorderEntry(abc.ABC):
    @abc.abstractmethod
    def apply(self, o: Observable.Observable) -> None: ...


class KeyRecorderEntry(RecorderEntry):
    def __init__(self, accessor: Accessor, key: str, item: typing.Any):
        self.accessor = accessor
        self.key = key
        self.item = item

    def apply(self, o: Observable.Observable) -> None:
        setattr(self.accessor.get(o), self.key, self.item)


class InsertRecorderEntry(RecorderEntry):
    def __init__(self, accessor: Accessor, key: str, index: int, item: typing.Any):
        self.accessor = accessor
        self.key = key
        self.index = index
        self.item = item

    def apply(self, o: Observable.Observable) -> None:
        self.accessor.get(o).insert_item(self.key, self.index, self.item)


class RemoveRecorderEntry(RecorderEntry):
    def __init__(self, accessor: Accessor, key: str, index: int):
        self.accessor = accessor
        self.key = key
        self.index = index

    def apply(self, o: Observable.Observable) -> None:
        self.accessor.get(o).remove_item(self.key, getattr(self.accessor.get(o), self.key)[self.index])


class Recorder:
    """Record changes to an observable object.

    The Accessor describe how to access the object from a root object.

    The RecorderEntry describes how to change the object.
    """

    # TODO: make changes resilient... what happens if underlying object changes and recorder can't be applied?
    # TODO: thread safety

    def __init__(self, object: typing.Any, accessor: typing.Optional[Accessor] = None, logger: typing.Optional[typing.List[RecorderEntry]] = None):
        self.__accessor = accessor or DirectAccessor()
        self.__logger: typing.List[RecorderEntry] = logger if logger is not None else list()
        self.__property_changed_event_listener = object.property_changed_event.listen(weak_partial(Recorder.__property_changed, self, weakref.ref(object)))
        self.__item_set_event_listener = object.item_set_event.listen(weak_partial(Recorder.__item_set, self))
        self.__item_cleared_event_listener = object.item_cleared_event.listen(weak_partial(Recorder.__item_cleared, self))
        self.__item_inserted_event_listener = object.item_inserted_event.listen(weak_partial(Recorder.__item_inserted, self))
        self.__item_removed_event_listener = object.item_removed_event.listen(weak_partial(Recorder.__item_removed, self))
        self.__item_recorders = dict()
        self.__relationship_recorders = collections.defaultdict(list)
        for key in object.item_names:
            item = getattr(object, key)
            if item:
                self.__item_recorders[key] = Recorder(item, KeyAccessor(self.__accessor, key), self.__logger)
        for key in object.relationship_names:
            items = getattr(object, key)
            for index, item in enumerate(items):
                self.__relationship_recorders[key].append(Recorder(item, IndexAccessor(KeyAccessor(self.__accessor, key), index), self.__logger))

    def close(self):
        self.__property_changed_event_listener.close()
        self.__property_changed_event_listener = None
        self.__item_set_event_listener.close()
        self.__item_set_event_listener = None
        self.__item_cleared_event_listener.close()
        self.__item_cleared_event_listener = None
        self.__item_inserted_event_listener.close()
        self.__item_inserted_event_listener = None
        self.__item_removed_event_listener.close()
        self.__item_removed_event_listener = None
        for key, item_recorder in self.__item_recorders.items():
            item_recorder.close()
        self.__item_recorders = None
        for key, relationship_recorder_list in self.__relationship_recorders.items():
            for relationship_recorder in self.__relationship_recorders[key]:
                relationship_recorder.close()
        self.__relationship_recorders = None

    def apply(self, object):
        for logger_item in self.__logger:
            logger_item.apply(object)

    @property
    def _accessor(self) -> Accessor:
        return self.__accessor

    @_accessor.setter
    def _accessor(self, value: Accessor) -> None:
        self.__accessor = value

    def __property_changed(self, o_ref: weakref.ReferenceType, key: str) -> None:
        object = o_ref()
        if object:
            if not hasattr(object, "_is_persistent_property_recordable") or object._is_persistent_property_recordable(key):
                self.__logger.append(KeyRecorderEntry(self.__accessor, key, getattr(object, key)))

    def __item_set(self, key: str, item: typing.Any) -> None:
        item_recorder = self.__item_recorders.pop(key)
        if item_recorder:
            item_recorder.close()
        if item:
            self.__item_recorders[key] = Recorder(item, KeyAccessor(self.__accessor, key), self.__logger)
        self.__logger.append(KeyRecorderEntry(self.__accessor, key, copy.deepcopy(item)))

    def __item_cleared(self, key: str) -> None:
        self.__item_set(key, None)

    def __item_inserted(self, key: str, value: typing.Any, before_index: int) -> None:
        for index, relationship_recorder in enumerate(self.__relationship_recorders[key]):
            if index >= before_index:
                relationship_recorder._accessor = IndexAccessor(KeyAccessor(self.__accessor, key), index + 1)
        self.__relationship_recorders[key].insert(before_index, Recorder(value, IndexAccessor(KeyAccessor(self.__accessor, key), before_index), self.__logger))
        self.__logger.append(InsertRecorderEntry(self.__accessor, key, before_index, copy.deepcopy(value)))

    def __item_removed(self, key: str, value: typing.Any, item_index: int) -> None:
        for index, relationship_recorder in enumerate(self.__relationship_recorders[key]):
            if index > item_index:
                relationship_recorder._accessor = IndexAccessor(KeyAccessor(self.__accessor, key), index - 1)
        self.__relationship_recorders[key].pop(item_index).close()
        self.__logger.append(RemoveRecorderEntry(self.__accessor, key, item_index))

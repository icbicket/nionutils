"""
    Model classes. Useful for bindings.
"""
from __future__ import annotations

# standard libraries
import asyncio
import operator
import typing

# third party libraries
# none

# local libraries
import weakref

from . import Event
from . import Observable
from . import Stream
from .ReferenceCounting import weak_partial

T = typing.TypeVar('T')
OptionalT = typing.Optional[T]


class PropertyModel(Observable.Observable, typing.Generic[T]):
    """Holds a value which can be observed for changes.

    The value can be any type that supports equality test.

    An optional on_value_changed method gets called when the value changes.
    """

    def __init__(self, value: OptionalT = None, cmp: typing.Optional[typing.Callable[[OptionalT, OptionalT], bool]] = None):
        super().__init__()
        self.__value = value
        self.__cmp = cmp if cmp else operator.eq
        self.on_value_changed : typing.Optional[typing.Callable[[OptionalT], None]] = None

    def close(self) -> None:
        self.on_value_changed = None

    @property
    def value(self) -> OptionalT:
        return self.__value

    @value.setter
    def value(self, value: OptionalT) -> None:
        if self.__value is None:
            not_equal = value is not None
        elif value is None:
            not_equal = self.__value is not None
        else:
            not_equal = not self.__cmp(value, self.__value)
        if not_equal:
            self._set_value(value)
            if self.on_value_changed:
                self.on_value_changed(value)

    def _set_value(self, value: OptionalT) -> None:
        self.__value = value
        self.notify_property_changed("value")


class FuncStreamValueModel(PropertyModel):
    """Converts a stream of functions to a property model, evaluated asynchronously, on a thread."""

    def __init__(self, value_func_stream: Stream.AbstractStream, event_loop: asyncio.AbstractEventLoop, value=None, cmp=None):
        super().__init__(value=value, cmp=cmp)
        self.__value_func_stream = value_func_stream.add_ref()
        self.__event_loop = event_loop
        self.__pending_task = Stream.StreamTask()
        self.__value_fn_ref = [lambda: None]
        self.__event = asyncio.Event()
        self.__evaluating = [False]

        async def update_value(event: asyncio.Event, evaluating: typing.List[bool], model_ref, value_fn_ref: typing.List) -> None:
            while True:
                await event.wait()
                evaluating[0] = True
                event.clear()
                value = None

                def eval():
                    nonlocal value
                    try:
                        value = value_fn_ref[0]()
                    except Exception as e:
                        pass

                await event_loop.run_in_executor(None, eval)
                model = model_ref()
                if model:
                    model.value = value
                evaluating[0] = event.is_set()

        self.__pending_task.create_task(update_value(self.__event, self.__evaluating, weakref.ref(self), self.__value_fn_ref))
        self.__stream_listener = value_func_stream.value_stream.listen(weak_partial(FuncStreamValueModel.__handle_value_func, self))
        self.__handle_value_func(self.__value_func_stream.value)

    def close(self):
        self.__pending_task.clear()
        self.__pending_task = None
        self.__stream_listener.close()
        self.__stream_listener = None
        self.__value_func_stream.remove_ref()
        self.__value_func_stream = None
        self.__event_loop = None
        super().close()

    def _run_until_complete(self):
        while True:
            self.__event_loop.stop()
            self.__event_loop.run_forever()
            if not self.__evaluating[0]:
                break

    def _evaluate_immediate(self):
        return self.__value_func_stream.value()

    def __handle_value_func(self, value_func: typing.Callable[[], typing.Any]) -> None:
        self.__value_fn_ref[0] = value_func
        self.__event.set()


class StreamValueModel(PropertyModel):
    """Converts a stream to a property model."""

    def __init__(self, value_stream: Stream.AbstractStream, value=None, cmp=None):
        super().__init__(value=value, cmp=cmp)
        self.__value_stream = value_stream.add_ref()

        def handle_value(model: StreamValueModel, value: typing.Any) -> None:
            model.value = value

        self.__stream_listener = value_stream.value_stream.listen(weak_partial(handle_value, self))

        handle_value(self, value_stream.value)

    def close(self):
        self.__stream_listener.close()
        self.__stream_listener = None
        self.__value_stream.remove_ref()
        self.__value_stream = None
        super().close()


class PropertyChangedPropertyModel(PropertyModel[T], typing.Generic[T]):
    """Observes a property on another item and makes it a standard property model.

    When the observed property changes, update this value.

    When this value changes, update the observed property.
    """

    def __init__(self, observable: Observable.Observable, property_name: str):
        super().__init__(getattr(observable, property_name, None))
        self.__observable = observable
        self.__property_name = property_name

        def property_changed(property_model: PropertyChangedPropertyModel, observable: Observable.Observable, property_name: str, property_name_: str) -> None:
            # check if changed property matches property name for this object
            if property_name_ == property_name:
                property_model.value = getattr(observable, property_name)

        self.__listener = self.__observable.property_changed_event.listen(weak_partial(property_changed, self, observable, property_name))

    def close(self) -> None:
        self.__listener.close()
        self.__listener = typing.cast(Event.EventListener, None)
        super().close()

    def _set_value(self, value: OptionalT) -> None:
        super()._set_value(value)
        # set the property on the observed object. this will trigger a property changed, but will be ignored since
        # the value doesn't change.
        setattr(self.__observable, self.__property_name, value)

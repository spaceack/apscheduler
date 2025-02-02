from __future__ import annotations

import logging
from abc import abstractmethod
from asyncio import iscoroutinefunction
from concurrent.futures.thread import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from inspect import isawaitable, isclass
from logging import Logger
from typing import Any, Callable, Dict, FrozenSet, Iterable, NewType, Optional, Set, Type, Union
from uuid import UUID

import attr
from anyio import create_task_group
from anyio.abc import TaskGroup

from . import abc

SubscriptionToken = NewType('SubscriptionToken', object)


def timestamp_to_datetime(value: Union[datetime, float, None]) -> Optional[datetime]:
    if isinstance(value, float):
        return datetime.fromtimestamp(value, timezone.utc)

    return value


@attr.define(kw_only=True, frozen=True)
class Event:
    timestamp: datetime = attr.field(factory=partial(datetime.now, timezone.utc),
                                     converter=timestamp_to_datetime)


#
# Data store events
#

@attr.define(kw_only=True, frozen=True)
class DataStoreEvent(Event):
    pass


@attr.define(kw_only=True, frozen=True)
class ScheduleAdded(DataStoreEvent):
    schedule_id: str
    next_fire_time: Optional[datetime] = attr.field(converter=timestamp_to_datetime)


@attr.define(kw_only=True, frozen=True)
class ScheduleUpdated(DataStoreEvent):
    schedule_id: str
    next_fire_time: Optional[datetime] = attr.field(converter=timestamp_to_datetime)


@attr.define(kw_only=True, frozen=True)
class ScheduleRemoved(DataStoreEvent):
    schedule_id: str


@attr.define(kw_only=True, frozen=True)
class JobAdded(DataStoreEvent):
    job_id: UUID
    task_id: str
    schedule_id: Optional[str]
    tags: FrozenSet[str]


@attr.define(kw_only=True, frozen=True)
class JobRemoved(DataStoreEvent):
    job_id: UUID


@attr.define(kw_only=True, frozen=True)
class ScheduleDeserializationFailed(DataStoreEvent):
    schedule_id: str
    exception: BaseException


@attr.define(kw_only=True, frozen=True)
class JobDeserializationFailed(DataStoreEvent):
    job_id: UUID
    exception: BaseException


#
# Scheduler events
#

@attr.define(kw_only=True, frozen=True)
class SchedulerEvent(Event):
    pass


@attr.define(kw_only=True, frozen=True)
class SchedulerStarted(SchedulerEvent):
    pass


@attr.define(kw_only=True, frozen=True)
class SchedulerStopped(SchedulerEvent):
    exception: Optional[BaseException] = None


#
# Worker events
#

@attr.define(kw_only=True, frozen=True)
class WorkerEvent(Event):
    pass


@attr.define(kw_only=True, frozen=True)
class WorkerStarted(WorkerEvent):
    pass


@attr.define(kw_only=True, frozen=True)
class WorkerStopped(WorkerEvent):
    exception: Optional[BaseException] = None


@attr.define(kw_only=True, frozen=True)
class JobExecutionEvent(WorkerEvent):
    job_id: UUID
    task_id: str
    schedule_id: Optional[str]
    scheduled_fire_time: Optional[datetime]
    start_deadline: Optional[datetime]
    start_time: datetime


@attr.define(kw_only=True, frozen=True)
class JobStarted(JobExecutionEvent):
    """Signals that a worker has started running a job."""


@attr.define(kw_only=True, frozen=True)
class JobDeadlineMissed(JobExecutionEvent):
    """Signals that a worker has skipped a job because its deadline was missed."""


@attr.define(kw_only=True, frozen=True)
class JobCompleted(JobExecutionEvent):
    """Signals that a worker has successfully run a job."""
    return_value: Any


@attr.define(kw_only=True, frozen=True)
class JobFailed(JobExecutionEvent):
    """Signals that a worker encountered an exception while running a job."""
    exception: str
    traceback: str


_all_event_types = [x for x in locals().values() if isclass(x) and issubclass(x, Event)]


#
# Event delivery
#

@dataclass(eq=False, frozen=True)
class Subscription:
    callback: Callable[[Event], Any]
    event_types: Optional[Set[Type[Event]]]


@dataclass
class _BaseEventHub(abc.EventSource):
    _logger: Logger = field(init=False, default_factory=lambda: logging.getLogger(__name__))
    _subscriptions: Dict[SubscriptionToken, Subscription] = field(init=False, default_factory=dict)

    def subscribe(self, callback: Callable[[Event], Any],
                  event_types: Optional[Iterable[Type[Event]]] = None) -> SubscriptionToken:
        types = set(event_types) if event_types else None
        token = SubscriptionToken(object())
        subscription = Subscription(callback, types)
        self._subscriptions[token] = subscription
        return token

    def unsubscribe(self, token: SubscriptionToken) -> None:
        self._subscriptions.pop(token, None)

    @abstractmethod
    def publish(self, event: Event) -> None:
        """Publish an event to all subscribers."""

    def relay_events_from(self, source: abc.EventSource) -> SubscriptionToken:
        return source.subscribe(self.publish)


class EventHub(_BaseEventHub):
    _executor: ThreadPoolExecutor

    def __enter__(self) -> EventHub:
        self._executor = ThreadPoolExecutor(1)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._executor.shutdown(wait=exc_type is None)

    def subscribe(self, callback: Callable[[Event], Any],
                  event_types: Optional[Iterable[Type[Event]]] = None) -> SubscriptionToken:
        if iscoroutinefunction(callback):
            raise ValueError('Coroutine functions are not supported as callbacks on a synchronous '
                             'event source')

        return super().subscribe(callback, event_types)

    def publish(self, event: Event) -> None:
        def deliver_event(func: Callable[[Event], Any]) -> None:
            try:
                func(event)
            except BaseException:
                self._logger.exception('Error delivering %s event', event.__class__.__name__)

        event_type = type(event)
        for subscription in list(self._subscriptions.values()):
            if subscription.event_types is None or event_type in subscription.event_types:
                self._executor.submit(deliver_event, subscription.callback)


class AsyncEventHub(_BaseEventHub):
    _task_group: TaskGroup

    async def __aenter__(self) -> AsyncEventHub:
        self._task_group = create_task_group()
        await self._task_group.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._task_group.__aexit__(exc_type, exc_val, exc_tb)
        del self._task_group

    def publish(self, event: Event) -> None:
        async def deliver_event(func: Callable[[Event], Any]) -> None:
            try:
                retval = func(event)
                if isawaitable(retval):
                    await retval
            except BaseException:
                self._logger.exception('Error delivering %s event', event.__class__.__name__)

        event_type = type(event)
        for subscription in self._subscriptions.values():
            if subscription.event_types is None or event_type in subscription.event_types:
                self._task_group.start_soon(deliver_event, subscription.callback)

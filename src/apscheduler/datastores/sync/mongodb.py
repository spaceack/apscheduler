from __future__ import annotations

import logging
from contextlib import ExitStack
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, List, Optional, Set, Tuple, Type
from uuid import UUID

from pymongo import ASCENDING, DeleteOne, MongoClient, UpdateOne
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError

from ... import events
from ...abc import DataStore, Job, Schedule, Serializer
from ...events import (
    DataStoreEvent, EventHub, JobAdded, ScheduleAdded, ScheduleRemoved, ScheduleUpdated,
    SubscriptionToken)
from ...exceptions import ConflictingIdError, DeserializationError, SerializationError
from ...policies import ConflictPolicy
from ...serializers.pickle import PickleSerializer
from ...util import reentrant


@reentrant
class MongoDBDataStore(DataStore):
    def __init__(self, client: MongoClient, *, serializer: Optional[Serializer] = None,
                 database: str = 'apscheduler', schedules_collection: str = 'schedules',
                 jobs_collection: str = 'jobs', lock_expiration_delay: float = 30,
                 start_from_scratch: bool = False):
        super().__init__()
        if not client.delegate.codec_options.tz_aware:
            raise ValueError('MongoDB client must have tz_aware set to True')

        self.client = client
        self.serializer = serializer or PickleSerializer()
        self.lock_expiration_delay = lock_expiration_delay
        self.start_from_scratch = start_from_scratch
        self._database = client[database]
        self._schedules: Collection = self._database[schedules_collection]
        self._jobs: Collection = self._database[jobs_collection]
        self._logger = logging.getLogger(__name__)
        self._exit_stack = ExitStack()
        self._events = EventHub()

    @classmethod
    def from_url(cls, uri: str, **options) -> 'MongoDBDataStore':
        client = MongoClient(uri)
        return cls(client, **options)

    def __enter__(self):
        server_info = self.client.server_info()
        if server_info['versionArray'] < [4, 0]:
            raise RuntimeError(f"MongoDB server must be at least v4.0; current version = "
                               f"{server_info['version']}")

        self._exit_stack.__enter__()
        self._exit_stack.enter_context(self._events)

        if self.start_from_scratch:
            self._schedules.delete_many({})
            self._jobs.delete_many({})

        self._schedules.create_index('next_fire_time')
        self._jobs.create_index('task_id')
        self._jobs.create_index('created_at')
        self._jobs.create_index('tags')
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._exit_stack.__exit__(exc_type, exc_val, exc_tb)

    def subscribe(self, callback: Callable[[events.Event], Any],
                  event_types: Optional[Iterable[Type[events.Event]]] = None) -> SubscriptionToken:
        return self._events.subscribe(callback, event_types)

    def unsubscribe(self, token: events.SubscriptionToken) -> None:
        self._events.unsubscribe(token)

    def get_schedules(self, ids: Optional[Set[str]] = None) -> List[Schedule]:
        schedules: List[Schedule] = []
        filters = {'_id': {'$in': list(ids)}} if ids is not None else {}
        cursor = self._schedules.find(filters, projection=['_id', 'serialized_data']).sort('_id')
        for document in cursor:
            try:
                schedule = self.serializer.deserialize(document['serialized_data'])
            except DeserializationError:
                self._logger.warning('Failed to deserialize schedule %r', document['_id'])
                continue

            schedules.append(schedule)

        return schedules

    def add_schedule(self, schedule: Schedule, conflict_policy: ConflictPolicy) -> None:
        event: DataStoreEvent
        serialized_data = self.serializer.serialize(schedule)
        document = {
            '_id': schedule.id,
            'task_id': schedule.task_id,
            'serialized_data': serialized_data,
            'next_fire_time': schedule.next_fire_time
        }
        try:
            self._schedules.insert_one(document)
        except DuplicateKeyError:
            if conflict_policy is ConflictPolicy.exception:
                raise ConflictingIdError(schedule.id) from None
            elif conflict_policy is ConflictPolicy.replace:
                self._schedules.replace_one({'_id': schedule.id}, document, True)
                event = ScheduleUpdated(
                    schedule_id=schedule.id,
                    next_fire_time=schedule.next_fire_time)
                self._events.publish(event)
        else:
            event = ScheduleAdded(schedule_id=schedule.id,
                                  next_fire_time=schedule.next_fire_time)
            self._events.publish(event)

    def remove_schedules(self, ids: Iterable[str]) -> None:
        with self.client.start_session() as s, s.start_transaction():
            filters = {'_id': {'$in': list(ids)}} if ids is not None else {}
            cursor = self._schedules.find(filters, projection=['_id'])
            ids = [doc['_id'] for doc in cursor]
            if ids:
                self._schedules.delete_many(filters)

        for schedule_id in ids:
            self._events.publish(ScheduleRemoved(schedule_id=schedule_id))

    def acquire_schedules(self, scheduler_id: str, limit: int) -> List[Schedule]:
        schedules: List[Schedule] = []
        with self.client.start_session() as s, s.start_transaction():
            cursor = self._schedules.find(
                {'next_fire_time': {'$ne': None},
                 '$or': [{'acquired_until': {'$exists': False}},
                         {'acquired_until': {'$lt': datetime.now(timezone.utc)}}]
                 },
                projection=['serialized_data']
            ).sort('next_fire_time').limit(limit)
            for document in cursor:
                schedule = self.serializer.deserialize(document['serialized_data'])
                schedules.append(schedule)

            if schedules:
                now = datetime.now(timezone.utc)
                acquired_until = datetime.fromtimestamp(
                    now.timestamp() + self.lock_expiration_delay, now.tzinfo)
                filters = {'_id': {'$in': [schedule.id for schedule in schedules]}}
                update = {'$set': {'acquired_by': scheduler_id,
                                   'acquired_until': acquired_until}}
                self._schedules.update_many(filters, update)

        return schedules

    def release_schedules(self, scheduler_id: str, schedules: List[Schedule]) -> None:
        updated_schedules: List[Tuple[str, datetime]] = []
        finished_schedule_ids: List[str] = []
        with self.client.start_session() as s, s.start_transaction():
            # Update schedules that have a next fire time
            requests = []
            for schedule in schedules:
                filters = {'_id': schedule.id, 'acquired_by': scheduler_id}
                if schedule.next_fire_time is not None:
                    try:
                        serialized_data = self.serializer.serialize(schedule)
                    except SerializationError:
                        self._logger.exception('Error serializing schedule %r – '
                                               'removing from data store', schedule.id)
                        requests.append(DeleteOne(filters))
                        finished_schedule_ids.append(schedule.id)
                        continue

                    update = {
                        '$unset': {
                            'acquired_by': True,
                            'acquired_until': True,
                        },
                        '$set': {
                            'next_fire_time': schedule.next_fire_time,
                            'serialized_data': serialized_data
                        }
                    }
                    requests.append(UpdateOne(filters, update))
                    updated_schedules.append((schedule.id, schedule.next_fire_time))
                else:
                    requests.append(DeleteOne(filters))
                    finished_schedule_ids.append(schedule.id)

            if requests:
                self._schedules.bulk_write(requests, ordered=False)
                for schedule_id, next_fire_time in updated_schedules:
                    event = ScheduleUpdated(schedule_id=schedule_id, next_fire_time=next_fire_time)
                    self._events.publish(event)

        for schedule_id in finished_schedule_ids:
            self._events.publish(ScheduleRemoved(schedule_id=schedule_id))

    def add_job(self, job: Job) -> None:
        serialized_data = self.serializer.serialize(job)
        document = {
            '_id': job.id,
            'serialized_data': serialized_data,
            'task_id': job.task_id,
            'created_at': datetime.now(timezone.utc),
            'tags': list(job.tags)
        }
        self._jobs.insert_one(document)
        event = JobAdded(job_id=job.id, task_id=job.task_id, schedule_id=job.schedule_id,
                         tags=job.tags)
        self._events.publish(event)

    def get_jobs(self, ids: Optional[Iterable[UUID]] = None) -> List[Job]:
        jobs: List[Job] = []
        filters = {'_id': {'$in': list(ids)}} if ids is not None else {}
        cursor = self._jobs.find(filters, projection=['_id', 'serialized_data']).sort('_id')
        for document in cursor:
            try:
                job = self.serializer.deserialize(document['serialized_data'])
            except DeserializationError:
                self._logger.warning('Failed to deserialize job %r', document['_id'])
                continue

            jobs.append(job)

        return jobs

    def acquire_jobs(self, worker_id: str, limit: Optional[int] = None) -> List[Job]:
        jobs: List[Job] = []
        with self.client.start_session() as s, s.start_transaction():
            cursor = self._jobs.find(
                {'$or': [{'acquired_until': {'$exists': False}},
                         {'acquired_until': {'$lt': datetime.now(timezone.utc)}}]
                 },
                projection=['serialized_data'],
                sort=[('created_at', ASCENDING)],
                limit=limit
            )
            for document in cursor:
                job = self.serializer.deserialize(document['serialized_data'])
                jobs.append(job)

            if jobs:
                now = datetime.now(timezone.utc)
                acquired_until = datetime.fromtimestamp(
                    now.timestamp() + self.lock_expiration_delay, timezone.utc)
                filters = {'_id': {'$in': [job.id for job in jobs]}}
                update = {'$set': {'acquired_by': worker_id,
                                   'acquired_until': acquired_until}}
                self._jobs.update_many(filters, update)
                return jobs

    def release_jobs(self, worker_id: str, jobs: List[Job]) -> None:
        filters = {'_id': {'$in': [job.id for job in jobs]}, 'acquired_by': worker_id}
        self._jobs.delete_many(filters)

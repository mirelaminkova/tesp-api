"""Repository: TaskRepository.get_tasks paging against a fake collection (issue #91; no services needed)."""

import asyncio

from bson.objectid import ObjectId
from pymonad.maybe import Just, Nothing

from tesp_api.repository.task_repository import TaskRepository


class FakeCursor:
    def __init__(self, docs, query):
        after = query.get('_id', {}).get('$gt')
        self.docs = [d for d in docs if after is None or d['_id'] > after]
        self.sorted = None

    def sort(self, key, direction):
        self.sorted = (key, direction)
        self.docs.sort(key=lambda d: d[key], reverse=direction < 0)
        return self

    def limit(self, n):
        self.docs = self.docs[:n]
        return self

    async def to_list(self, _):
        return self.docs


class FakeTasks:
    def __init__(self, n):
        # Stored out of order: paging must not rely on natural order.
        self.docs = [{'_id': ObjectId(), 'executors': [], 'author': 'a'} for _ in range(n)][::-1]
        self.cursors = []

    def find(self, query):
        cursor = FakeCursor(self.docs, query)
        self.cursors.append(cursor)
        return cursor


def _page(repo, size, token):
    async def page():
        return await repo.get_tasks(Nothing, Just(size), token)
    return asyncio.run(page())


def test_pages_are_ordered_complete_and_end_without_token():
    repo = TaskRepository()
    repo._tasks = FakeTasks(5)
    ids, token = [], Nothing
    for _ in range(10):
        tasks, next_token = _page(repo, 2, token)
        ids += [t.id for t in tasks]
        if next_token is None:
            break
        token = Just(next_token)
    assert ids == sorted(d['_id'] for d in repo._tasks.docs)
    assert all(c.sorted == ('_id', 1) for c in repo._tasks.cursors)


def test_short_page_has_no_token():
    repo = TaskRepository()
    repo._tasks = FakeTasks(3)
    tasks, next_token = _page(repo, 5, Nothing)
    assert len(tasks) == 3 and next_token is None

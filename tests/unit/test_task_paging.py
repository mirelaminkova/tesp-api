"""ListTasks paging (issue #91): pages follow `_id` order, every task appears exactly once, the last page has no
`next_page_token` (never the string "None"), and an invalid token is a 400.

MongoDB returns unsorted results in natural (storage) order, which need not follow `_id`. The fake collection below
keeps its own natural order, different from `_id` order, so paging that forgets to sort skips or repeats tasks.
"""
import pytest
from bson.objectid import ObjectId
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tesp_api.api.api import api_router
from tesp_api.repository.task_repository import task_repository

PAGE_SIZE = 3


class FakeCursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, key, direction=1):
        self.docs = sorted(self.docs, key=lambda d: d[key], reverse=direction < 0)
        return self

    def limit(self, n):
        self.docs = self.docs[:n] if n else self.docs
        return self

    async def to_list(self, _length):
        return list(self.docs)


class FakeTasks:
    """A `tasks` collection whose natural order is not `_id` order: new documents land at the front (reused space)."""

    def __init__(self):
        self.docs = []

    def insert(self, name):
        doc = {'_id': ObjectId(), 'name': name, 'executors': [], 'state': 'QUEUED'}
        self.docs.insert(0, doc)
        return doc['_id']

    def find(self, query):
        def matches(doc):
            after = query.get('_id', {}).get('$gt')
            prefix = query.get('name', {}).get('$regex', '^')[1:]
            return (after is None or doc['_id'] > after) and doc['name'].startswith(prefix)
        return FakeCursor([d for d in self.docs if matches(d)])


@pytest.fixture
def tasks(monkeypatch):
    fake = FakeTasks()
    monkeypatch.setattr(task_repository, '_tasks', fake)
    return fake


@pytest.fixture
def client():
    # Only the API routes: the full app connects to MongoDB and Pulsar on import and startup.
    app = FastAPI()
    app.include_router(api_router)
    return TestClient(app)


def list_page(client, token=None, **params):
    params = {'page_size': PAGE_SIZE, **params}
    if token is not None:
        params['page_token'] = token
    response = client.get('/v1/tasks', params=params)
    assert response.status_code == 200, response.text
    return response.json()


def walk(client, between_pages=lambda page_no: None, max_pages=50, **params):
    """Follows `next_page_token` to the end; returns the ids per page and every token seen."""
    pages, tokens, token = [], [], None
    for page_no in range(max_pages):
        body = list_page(client, token, **params)
        pages.append([t['id'] for t in body['tasks']])
        token = body.get('next_page_token')
        tokens.append(token)
        if not token:
            return pages, tokens
        between_pages(page_no)
    pytest.fail(f"no last page after {max_pages} pages")


@pytest.mark.parametrize('count', [0, 1, PAGE_SIZE - 1, PAGE_SIZE, 2 * PAGE_SIZE, 2 * PAGE_SIZE + 1])
def test_last_page_has_no_token(client, tasks, count):
    for i in range(count):
        tasks.insert(f"t{i}")
    pages, tokens = walk(client)
    assert tokens[-1] in (None, ''), tokens
    assert 'None' not in tokens
    assert sum(map(len, pages)) == count


def test_short_page_has_no_token(client, tasks):
    for i in range(PAGE_SIZE - 1):
        tasks.insert(f"t{i}")
    body = list_page(client)
    assert len(body['tasks']) == PAGE_SIZE - 1
    assert not body.get('next_page_token')


def test_walk_visits_every_task_once_in_id_order(client, tasks):
    created = [str(tasks.insert(f"t{i}")) for i in range(3 * PAGE_SIZE + 1)]
    pages, _ = walk(client)
    seen = [task_id for page in pages for task_id in page]
    assert seen == sorted(created, key=ObjectId)
    assert all(0 < len(page) <= PAGE_SIZE for page in pages[:-1])


def test_walk_with_interleaved_inserts_visits_every_task_once(client, tasks):
    created = [str(tasks.insert(f"t{i}")) for i in range(2 * PAGE_SIZE + 2)]

    def insert_two(page_no):
        created.extend(str(tasks.insert(f"late{page_no}-{j}")) for j in range(2))

    pages, _ = walk(client, between_pages=insert_two)
    seen = [task_id for page in pages for task_id in page]
    assert len(seen) == len(set(seen)), "a task was listed twice"
    assert set(seen) == set(created), "a task was skipped"
    assert seen == sorted(seen, key=ObjectId), "pages are not in stable _id order"


def test_walk_with_name_prefix_keeps_order(client, tasks):
    created = [str(tasks.insert(f"{'keep' if i % 2 else 'drop'}{i}")) for i in range(4 * PAGE_SIZE)]
    pages, _ = walk(client, name_prefix='keep')
    seen = [task_id for page in pages for task_id in page]
    assert seen == sorted(created[1::2], key=ObjectId)


@pytest.mark.parametrize('token', ['None', 'not-an-object-id', '123'])
def test_invalid_token_is_bad_request(client, tasks, token):
    tasks.insert('t0')
    response = client.get('/v1/tasks', params={'page_size': PAGE_SIZE, 'page_token': token})
    assert response.status_code == 400, response.text

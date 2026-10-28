import pytest

from tesp_api.repository.model.task import TesTaskState
from tesp_api.repository.task_query import list_tasks_query

ANY_VALUE = {"$type": "string"}


def test_no_filters_match_everything():
    assert list_tasks_query() == {}
    assert list_tasks_query(name_prefix="", tag_keys=[], tag_values=[]) == {}


def test_state_filter():
    assert list_tasks_query(state=TesTaskState.COMPLETE) == {'state': TesTaskState.COMPLETE}


def test_name_prefix_filter_is_unchanged():
    assert list_tasks_query(name_prefix="job") == {'name': {'$regex': "^job"}}


def test_tag_key_with_value():
    assert list_tasks_query(tag_keys=["foo"], tag_values=["bar"]) == {"tags.foo": "bar"}


@pytest.mark.parametrize("tag_values", [None, [], [""]])
def test_tag_key_without_value_matches_any_value(tag_values):
    assert list_tasks_query(tag_keys=["foo"], tag_values=tag_values) == {"tags.foo": ANY_VALUE}


def test_tag_keys_and_values_are_paired_by_position():
    query = list_tasks_query(tag_keys=["foo", "baz", "qux"], tag_values=["", "bar"])
    assert query == {'$and': [{"tags.foo": ANY_VALUE}, {"tags.baz": "bar"}, {"tags.qux": ANY_VALUE}]}


def test_all_filters_are_combined():
    query = list_tasks_query(name_prefix="job", state=TesTaskState.RUNNING, tag_keys=["foo"], tag_values=["bar"])
    assert query == {'$and': [{'name': {'$regex': "^job"}}, {'state': TesTaskState.RUNNING}, {"tags.foo": "bar"}]}


@pytest.mark.parametrize("key", ["a.b", "$where", "$k.x"])
def test_dotted_and_dollar_keys_are_taken_literally(key):
    tag = {"$getField": {"field": {"$literal": key}, "input": "$tags"}}
    assert list_tasks_query(tag_keys=[key], tag_values=["v"]) == {"$expr": {"$eq": [tag, "v"]}}
    assert list_tasks_query(tag_keys=[key]) == {"$expr": {"$eq": [{"$type": tag}, "string"]}}

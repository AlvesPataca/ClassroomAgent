import json
from unittest.mock import MagicMock, patch

import httplib2
import pytest
from googleapiclient.errors import HttpError

from app.classroom.client import ClassroomClient
from app.config import Settings
from app.errors import AppError


@pytest.mark.parametrize(
    "method,key,item",
    [
        ("list_courses", "courses", {"id": "1", "name": "Course"}),
        ("list_assignments", "courseWork", {"id": "1", "courseId": "c", "title": "Work"}),
        (
            "list_submissions",
            "studentSubmissions",
            {"id": "1", "courseId": "c", "courseWorkId": "a"},
        ),
    ],
)
def test_all_endpoints_paginate(method, key, item):
    service = MagicMock()
    resource = service.courses.return_value
    if method != "list_courses":
        resource = resource.courseWork.return_value
    if method == "list_submissions":
        resource = resource.studentSubmissions.return_value
    resource.list.return_value.execute.side_effect = [
        {key: [item], "nextPageToken": "page2"},
        {key: [dict(item, id="2")]},
    ]
    client = ClassroomClient(service, Settings())
    result = getattr(client, method)(*(() if method == "list_courses" else ("c",)))
    assert [row.google_id for row in result] == ["1", "2"]
    assert resource.list.call_args.kwargs["pageToken"] == "page2"
    if method == "list_courses":
        assert resource.list.call_args.kwargs["studentId"] == "me"
        assert resource.list.call_args.kwargs["courseStates"] == ["ACTIVE"]
    if method == "list_submissions":
        assert resource.list.call_args.kwargs["userId"] == "me"
        assert resource.list.call_args.kwargs["courseWorkId"] == "-"


@pytest.mark.parametrize(
    "code,retries", [(401, 0), (403, 0), (404, 0), (429, 2), (500, 2), (503, 2)]
)
def test_retry_and_safe_errors(code, retries):
    request = MagicMock()
    request.execute.side_effect = HttpError(httplib2.Response({"status": code}), b"SECRET_TOKEN")
    client = ClassroomClient(MagicMock(), Settings(max_retries=2))
    with patch("app.classroom.client.time.sleep") as sleep, pytest.raises(AppError) as caught:
        client._execute(request)
    assert request.execute.call_count == retries + 1
    assert sleep.call_count == retries
    assert "SECRET_TOKEN" not in str(caught.value)


def test_timeout_recovers():
    request = MagicMock()
    request.execute.side_effect = [TimeoutError(), {}]
    with patch("app.classroom.client.time.sleep"):
        assert ClassroomClient(MagicMock(), Settings())._execute(request) == {}


@pytest.mark.parametrize(
    "pages",
    [
        [
            {},
        ],
        [{"courses": "bad"}],
        [{"courses": [{"id": "1"}]}],
        [{"nextPageToken": "x"}, {"nextPageToken": "x"}],
    ],
)
def test_empty_invalid_and_repeated_pages(pages):
    service = MagicMock()
    service.courses.return_value.list.return_value.execute.side_effect = pages
    client = ClassroomClient(service, Settings())
    if pages == [{}]:
        assert client.list_courses() == []
    else:
        with pytest.raises(AppError):
            client.list_courses()


def test_real_discovery_schema_without_network():
    from google.auth.credentials import AnonymousCredentials

    client = ClassroomClient.from_credentials(AnonymousCredentials(), Settings())
    request = (
        client._service.courses()
        .courseWork()
        .studentSubmissions()
        .list(
            courseId="c",
            courseWorkId="-",
            userId="me",
            pageSize=100,
        )
    )
    assert request.method == "GET"
    assert "classroom.googleapis.com" in request.uri


@pytest.mark.parametrize(
    "field,reason,expected",
    [
        ("details", "SERVICE_DISABLED", "Google Classroom API desativada"),
        ("errors", "accessNotConfigured", "Google Classroom API desativada"),
        ("details", "ACCESS_TOKEN_SCOPE_INSUFFICIENT", "Permissões OAuth insuficientes"),
        ("errors", "insufficientPermissions", "Permissões OAuth insuficientes"),
        ("details", "SECRET_UNKNOWN_REASON", "Acesso negado (HTTP 403)"),
    ],
)
def test_structured_403_is_actionable_without_leaking(field, reason, expected):
    payload = {
        "error": {
            "message": "SECRET_MESSAGE",
            field: [{"reason": reason, "metadata": {"secret": "SECRET_METADATA"}}],
        }
    }
    request = MagicMock()
    request.execute.side_effect = HttpError(
        httplib2.Response({"status": 403}), json.dumps(payload).encode()
    )
    with pytest.raises(AppError) as caught:
        ClassroomClient(MagicMock(), Settings())._execute(request)
    assert expected in str(caught.value)
    assert "SECRET" not in str(caught.value)
    request.execute.assert_called_once()


@pytest.mark.parametrize(
    "payload",
    [
        b"invalid",
        b"[]",
        b'{"error":null}',
        b'{"error":{"details":null}}',
        b'{"error":{"errors":[null,42]}}',
    ],
)
def test_malformed_403_remains_safe(payload):
    from app.classroom.client import forbidden_message

    assert forbidden_message(payload).startswith("Acesso negado (HTTP 403)")

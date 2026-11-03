from __future__ import annotations

from io import BytesIO

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber

from app.documents.storage import LocalPrivateStorage, S3PrivateStorage


def test_local_private_storage_round_trips_and_deletes(tmp_path) -> None:
    storage = LocalPrivateStorage(tmp_path)

    key = storage.put(b"synthetic-private-document")

    assert storage.read(key) == b"synthetic-private-document"
    storage.delete(key)
    with pytest.raises(FileNotFoundError):
        storage.read(key)


@pytest.mark.parametrize("key", ["../secret", "folder/object", "folder\\object", "..", ""])
def test_storage_rejects_non_opaque_keys(key: str, tmp_path) -> None:
    storage = LocalPrivateStorage(tmp_path)

    with pytest.raises(FileNotFoundError):
        storage.read(key)


def test_s3_storage_uses_private_opaque_keys_and_encrypted_object_operations() -> None:
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="unit-test-access-key",
        aws_secret_access_key="unit-test-secret-key",
    )
    payload = b"synthetic-private-document"
    body = StreamingBody(BytesIO(payload), len(payload))
    with Stubber(client) as stubber:
        stubber.add_response(
            "put_object",
            {},
            {
                "Bucket": "private-test-bucket",
                "Key": ANY,
                "Body": payload,
                "ServerSideEncryption": "AES256",
            },
        )
        stubber.add_response(
            "get_object",
            {"Body": body, "ContentLength": len(payload)},
            {"Bucket": "private-test-bucket", "Key": ANY},
        )
        stubber.add_response(
            "delete_object",
            {},
            {"Bucket": "private-test-bucket", "Key": ANY},
        )
        storage = S3PrivateStorage(client=client, bucket="private-test-bucket")

        key = storage.put(payload)
        assert len(key) == 32 and key.isalnum()
        assert storage.read(key) == payload
        storage.delete(key)

        stubber.assert_no_pending_responses()


def test_s3_missing_object_is_reported_as_not_found() -> None:
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="unit-test-access-key",
        aws_secret_access_key="unit-test-secret-key",
    )
    with Stubber(client) as stubber:
        stubber.add_client_error(
            "get_object",
            service_error_code="NoSuchKey",
            service_message="missing",
            http_status_code=404,
            expected_params={"Bucket": "private-test-bucket", "Key": "a" * 32},
        )
        storage = S3PrivateStorage(client=client, bucket="private-test-bucket")

        with pytest.raises(FileNotFoundError):
            storage.read("a" * 32)

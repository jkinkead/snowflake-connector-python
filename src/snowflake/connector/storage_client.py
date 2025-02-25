#
# Copyright (c) 2012-2021 Snowflake Computing Inc. All right reserved.
#

from __future__ import division

import os
import shutil
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from collections import defaultdict, namedtuple
from io import BytesIO
from logging import getLogger
from math import ceil
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Tuple, Union

import OpenSSL

from .constants import FileHeader, ResultStatus
from .encryption_util import EncryptionMetadata, SnowflakeEncryptionUtil
from .errors import RequestExceedMaxRetryError
from .file_util import SnowflakeFileUtil
from .vendored import requests
from .vendored.requests import ConnectionError, Timeout

if TYPE_CHECKING:  # pragma: no cover
    from .file_transfer_agent import SnowflakeFileMeta, StorageCredential

logger = getLogger(__name__)

"""
Encryption Material
"""
SnowflakeFileEncryptionMaterial = namedtuple(
    "SnowflakeS3FileEncryptionMaterial",
    [
        "query_stage_master_key",  # query stage master key
        "query_id",  # query id
        "smk_id",  # SMK id
    ],
)

METHODS = {
    "GET": requests.get,
    "PUT": requests.put,
    "POST": requests.post,
    "HEAD": requests.head,
    "DELETE": requests.delete,
}


class SnowflakeStorageClient(ABC):
    TRANSIENT_HTTP_ERR = (408, 429, 500, 502, 503, 504)

    TRANSIENT_ERRORS = (OpenSSL.SSL.SysCallError, Timeout, ConnectionError)
    SLEEP_MAX = 16.0
    SLEEP_UNIT = 1.0

    def __init__(
        self,
        meta: "SnowflakeFileMeta",
        stage_info: Dict[str, Any],
        chunk_size: int,
        chunked_transfer: Optional[bool] = True,
        credentials: Optional["StorageCredential"] = None,
        max_retry: int = 5,
    ):
        self.meta = meta
        self.stage_info = stage_info
        self.retry_count: Dict[Union[int, str], int] = defaultdict(lambda: 0)
        self.tmp_dir = tempfile.mkdtemp()
        self.data_file: Optional[str] = None
        self.encryption_metadata: Optional["EncryptionMetadata"] = None

        self.max_retry = max_retry  # TODO
        self.credentials = credentials
        # UPLOAD
        meta.real_src_file_name = meta.src_file_name
        meta.upload_size = meta.src_file_size
        self.preprocessed = (
            False  # so we don't repeat compression/file digest when re-encrypting
        )
        # DOWNLOAD
        self.full_dst_file_name: Optional[str] = (
            os.path.realpath(
                os.path.join(
                    self.meta.local_location, os.path.basename(self.meta.dst_file_name)
                )
            )
            if self.meta.local_location
            else None
        )
        self.intermediate_dst_path: Optional[Path] = (
            Path(self.full_dst_file_name + ".part")
            if self.meta.local_location
            else None
        )
        # CHUNK
        self.chunked_transfer = chunked_transfer  # only true for GCS
        self.chunk_size = chunk_size
        self.num_of_chunks = 0
        self.lock = threading.Lock()
        self.successful_transfers: int = 0
        self.failed_transfers: int = 0
        # only used when PRESIGNED_URL expires
        self.last_err_is_presigned_url = False

    def compress(self):
        if self.meta.require_compress:
            meta = self.meta
            logger.debug(f"compressing file={meta.src_file_name}")
            if meta.src_stream:
                (
                    meta.real_src_stream,
                    upload_size,
                ) = SnowflakeFileUtil.compress_with_gzip_from_stream(meta.src_stream)
            else:
                (
                    meta.real_src_file_name,
                    upload_size,
                ) = SnowflakeFileUtil.compress_file_with_gzip(
                    meta.src_file_name, self.tmp_dir
                )

    def get_digest(self):
        meta = self.meta
        logger.debug(f"getting digest file={meta.real_src_file_name}")
        if meta.src_stream is None:
            (
                meta.sha256_digest,
                meta.upload_size,
            ) = SnowflakeFileUtil.get_digest_and_size_for_file(meta.real_src_file_name)
        else:
            (
                meta.sha256_digest,
                meta.upload_size,
            ) = SnowflakeFileUtil.get_digest_and_size_for_stream(
                meta.real_src_stream or meta.src_stream
            )

    def encrypt(self):
        meta = self.meta
        logger.debug(f"encrypting file={meta.real_src_file_name}")
        if meta.src_stream is None:
            (
                self.encryption_metadata,
                self.data_file,
            ) = SnowflakeEncryptionUtil.encrypt_file(
                meta.encryption_material,
                meta.real_src_file_name,
                tmp_dir=self.tmp_dir,
            )
            meta.upload_size = os.path.getsize(self.data_file)
        else:
            encrypted_stream = BytesIO()
            src_stream = meta.real_src_stream or meta.src_stream
            src_stream.seek(0)
            self.encryption_metadata = SnowflakeEncryptionUtil.encrypt_stream(
                meta.encryption_material, src_stream, encrypted_stream
            )
            src_stream.seek(0)
            meta.upload_size = encrypted_stream.seek(0, os.SEEK_END)
            encrypted_stream.seek(0)
            if meta.real_src_stream is not None:
                meta.real_src_stream.close()
            meta.real_src_stream = encrypted_stream
            self.data_file = meta.real_src_file_name

    @abstractmethod
    def get_file_header(self, filename: str) -> Optional[FileHeader]:
        """Check if file exists in target location and obtain file metadata if exists.

        Notes:
            Updates meta.result_status.
        """
        pass

    def preprocess(self) -> None:
        meta = self.meta
        logger.debug(f"Preprocessing {meta.src_file_name}")

        self.get_file_header(meta.dst_file_name)  # Check if file exists on remote

        if meta.result_status == ResultStatus.UPLOADED and not meta.overwrite:
            # Skipped
            logger.debug(
                f'file already exists location="{self.stage_info["location"]}", '
                f'file_name="{meta.dst_file_name}"'
            )
            meta.dst_file_size = 0
            meta.result_status = ResultStatus.SKIPPED
        else:
            # Uploading
            if meta.require_compress:
                self.compress()
            self.get_digest()

        self.preprocessed = True

    def prepare_upload(self) -> None:
        meta = self.meta

        if not self.preprocessed:
            self.preprocess()
        elif meta.encryption_material:
            # need to clean up previous encrypted file
            os.remove(self.data_file)

        logger.debug(f"Preparing to upload {meta.src_file_name}")

        if meta.encryption_material:
            self.encrypt()
        else:
            self.data_file = meta.real_src_file_name
        logger.debug("finished preprocessing")
        if meta.upload_size < meta.multipart_threshold or not self.chunked_transfer:
            self.num_of_chunks = 1
        else:
            self.num_of_chunks = ceil(meta.upload_size / self.chunk_size)
        logger.debug(f"number of chunks {self.num_of_chunks}")
        # clean up
        self.retry_count = {}

        for chunk_id in range(self.num_of_chunks):
            self.retry_count[chunk_id] = 0
        if self.chunked_transfer and self.num_of_chunks > 1:
            self._initiate_multipart_upload()

    def finish_upload(self) -> None:
        meta = self.meta
        if self.successful_transfers == self.num_of_chunks:
            if self.num_of_chunks > 1:
                self._complete_multipart_upload()
            meta.result_status = ResultStatus.UPLOADED
            meta.dst_file_size = meta.upload_size
            logger.debug(f"{meta.src_file_name} upload is completed.")
        else:
            # TODO: add more error details to result/meta
            meta.dst_file_size = 0
            logger.debug(f"{meta.src_file_name} upload is aborted.")
            if self.num_of_chunks > 1:
                self._abort_multipart_upload()
            meta.result_status = ResultStatus.ERROR

    @abstractmethod
    def _has_expired_token(self, response: requests.Response) -> bool:
        pass

    def _send_request_with_retry(
        self,
        verb: str,
        get_request_args: Callable[[], Tuple[bytes, Dict[str, Any]]],
        retry_id: int,
    ) -> requests.Response:
        rest_call = METHODS[verb]
        conn = None
        if self.meta.self and self.meta.self._cursor.connection:
            conn = self.meta.self._cursor.connection

        while self.retry_count[retry_id] < self.max_retry:
            cur_timestamp = self.credentials.timestamp
            url, rest_kwargs = get_request_args()
            try:
                if conn:
                    with conn._rest._use_requests_session(url) as session:
                        logger.debug(f"storage client request with session {session}")
                        response = session.request(verb, url, **rest_kwargs)
                else:
                    logger.debug("storage client request with new session")
                    response = rest_call(url, **rest_kwargs)

                if self._has_expired_presigned_url(response):
                    self._update_presigned_url()
                else:
                    self.last_err_is_presigned_url = False
                    if response.status_code in self.TRANSIENT_HTTP_ERR:
                        time.sleep(
                            min(
                                # TODO should SLEEP_UNIT come from the parent
                                #  SnowflakeConnection and be customizable by users?
                                (2 ** self.retry_count[retry_id]) * self.SLEEP_UNIT,
                                self.SLEEP_MAX,
                            )
                        )
                        self.retry_count[retry_id] += 1
                    elif self._has_expired_token(response):
                        self.credentials.update(cur_timestamp)
                    else:
                        return response
            except self.TRANSIENT_ERRORS as e:
                self.last_err_is_presigned_url = False
                time.sleep(
                    min(
                        (2 ** self.retry_count[retry_id]) * self.SLEEP_UNIT,
                        self.SLEEP_MAX,
                    )
                )
                logger.warning(f"{verb} with url {url} failed for transient error: {e}")
                self.retry_count[retry_id] += 1
        else:
            raise RequestExceedMaxRetryError(
                f"{verb} with url {url} failed for exceeding maximum retries."
            )

    def prepare_download(self) -> None:
        # TODO: add nicer error message for when target directory is not writeable
        #  but this should be done before we get here
        base_dir = os.path.dirname(self.full_dst_file_name)
        if not os.path.exists(base_dir):
            os.makedirs(base_dir)

        # HEAD
        file_header = self.get_file_header(self.meta.dst_file_name)

        if file_header and file_header.encryption_metadata:
            self.encryption_metadata = file_header.encryption_metadata

        self.num_of_chunks = 1
        if file_header and file_header.content_length:
            self.meta.src_file_size = file_header.content_length
            if (
                self.chunked_transfer
                and self.meta.src_file_size > self.meta.multipart_threshold
            ):
                self.num_of_chunks = ceil(file_header.content_length / self.chunk_size)

        # Preallocate encrypted file.
        with self.intermediate_dst_path.open("wb+") as fd:
            fd.truncate(self.meta.src_file_size)

    def write_downloaded_chunk(self, chunk_id: int, chunk_data: bytes) -> None:
        # TODO: should we use chunking and write content in smaller chunks?
        with self.intermediate_dst_path.open("rb+") as fd:
            fd.seek(self.chunk_size * chunk_id)
            fd.write(chunk_data)

    def finish_download(self) -> None:
        meta = self.meta
        if self.num_of_chunks != 0 and self.successful_transfers == self.num_of_chunks:
            meta.result_status = ResultStatus.DOWNLOADED
            if meta.encryption_material:
                logger.debug(f"encrypted data file={self.full_dst_file_name}")
                # For storage utils that do not have the privilege of
                # getting the metadata early, both object and metadata
                # are downloaded at once. In which case, the file meta will
                # be updated with all the metadata that we need and
                # then we can call get_file_header to get just that and also
                # preserve the idea of getting metadata in the first place.
                # One example of this is the utils that use presigned url
                # for upload/download and not the storage client library.
                if meta.presigned_url is not None:
                    file_header = self.get_file_header(meta.src_file_name)
                    self.encryption_metadata = file_header.encryption_metadata

                tmp_dst_file_name = SnowflakeEncryptionUtil.decrypt_file(
                    self.encryption_metadata,
                    meta.encryption_material,
                    str(self.intermediate_dst_path),
                    tmp_dir=self.tmp_dir,
                )
                shutil.move(tmp_dst_file_name, self.full_dst_file_name)
                self.intermediate_dst_path.unlink()
            else:
                logger.debug(f"not encrypted data file={self.full_dst_file_name}")
                shutil.move(str(self.intermediate_dst_path), self.full_dst_file_name)
            stat_info = os.stat(self.full_dst_file_name)
            meta.dst_file_size = stat_info.st_size
        else:
            # TODO: add more error details to result/meta
            if os.path.isfile(self.full_dst_file_name):
                os.unlink(self.full_dst_file_name)
            logger.exception(f"Failed to download a file: {self.full_dst_file_name}")
            meta.dst_file_size = -1
            meta.result_status = ResultStatus.ERROR

    def upload_chunk(self, chunk_id: int) -> None:
        fd = (
            self.meta.real_src_stream
            or self.meta.src_stream
            or open(self.data_file, "rb")
        )
        with fd:
            if self.num_of_chunks == 1:
                _data = fd.read()
            else:
                fd.seek(chunk_id * self.chunk_size)
                _data = fd.read(self.chunk_size)
        logger.debug(f"Uploading chunk {chunk_id} of file {self.data_file}")
        self._upload_chunk(chunk_id, _data)
        logger.debug(f"Successfully uploaded chunk {chunk_id} of file {self.data_file}")

    @abstractmethod
    def _upload_chunk(self, chunk_id: int, chunk: bytes) -> None:
        pass

    @abstractmethod
    def download_chunk(self, chunk_id: int) -> None:
        pass

    # Override in GCS
    def _has_expired_presigned_url(self, response: requests.Response) -> bool:
        return False

    # Override in GCS
    def _update_presigned_url(self) -> None:
        pass

    # Override in S3
    def _initiate_multipart_upload(self) -> None:
        pass

    # Override in S3
    def _complete_multipart_upload(self) -> None:
        pass

    # Override in S3
    def _abort_multipart_upload(self) -> None:
        pass

    def delete_client_data(self):
        """Deletes the tmp_dir and closes the source stream belonging to this client.
        This function is idempotent."""
        if os.path.exists(self.tmp_dir):
            logger.debug(f"cleaning up tmp dir: {self.tmp_dir}")
            shutil.rmtree(self.tmp_dir)
        if self.meta.real_src_stream and not self.meta.real_src_stream.closed:
            self.meta.real_src_stream.close()

    def __del__(self):
        self.delete_client_data()

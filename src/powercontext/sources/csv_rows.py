# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Offline CSV row capture and exact primary-key evidence projection."""

from __future__ import annotations

import csv
import io
import json
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, model_validator

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


def _validate_document_id(document_id: str) -> str:
    if not isinstance(document_id, str) or not document_id or not document_id.strip():
        raise ValueError("document_id must be a non-empty string")
    if document_id != document_id.strip():
        raise ValueError("document_id must not contain leading or trailing whitespace")
    return document_id


def _validate_primary_key_name(primary_key: str) -> str:
    if not isinstance(primary_key, str) or not primary_key or not primary_key.strip():
        raise ValueError("primary_key must be a non-empty string")
    return primary_key


def _validate_primary_key_value(primary_key_value: str) -> str:
    if not isinstance(primary_key_value, str) or not primary_key_value or not primary_key_value.strip():
        raise ValueError("primary_key_value must be a non-empty, non-whitespace string")
    return primary_key_value


def _validate_columns_and_values(
    columns: tuple[str, ...],
    values: tuple[str, ...],
    primary_key: str,
    primary_key_value: str,
) -> None:
    if not columns:
        raise ValueError("columns must not be empty")
    if len(columns) != len(values):
        raise ValueError("values length must match columns length")
    seen_columns: set[str] = set()
    for col in columns:
        if not isinstance(col, str) or not col or not col.strip():
            raise ValueError("column names must be non-empty and not pure whitespace")
        if col in seen_columns:
            raise ValueError(f"duplicate column name: {col!r}")
        seen_columns.add(col)
    if primary_key not in seen_columns:
        raise ValueError(f"primary_key {primary_key!r} not found in columns")
    pk_idx = columns.index(primary_key)
    if values[pk_idx] != primary_key_value:
        raise ValueError("primary_key_value does not match value at primary_key column")


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Build a delimiter-safe, bounded source_id for one CSV row."""

    _validate_document_id(document_id)
    _validate_primary_key_value(primary_key_value)
    encoded_doc = quote(document_id, safe="")
    encoded_pk = quote(primary_key_value, safe="")
    source_id = f"csv:{encoded_doc}:{encoded_pk}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"csv row source_id must not exceed {MAX_SOURCE_ID_LENGTH} characters")
    return source_id


class CSVDocumentInput(BaseModel):
    """Complete offline CSV payload and its row identity configuration."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    primary_key: str
    content: bytes


class CSVRowInput(BaseModel):
    """Parsed row input preserving original header order and string values."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_row(self) -> CSVRowInput:
        _validate_document_id(self.document_id)
        _validate_primary_key_name(self.primary_key)
        _validate_primary_key_value(self.primary_key_value)
        _validate_columns_and_values(
            self.columns,
            self.values,
            self.primary_key,
            self.primary_key_value,
        )
        csv_row_source_id(self.document_id, self.primary_key_value)
        return self


class CSVRowSource(Source):
    """Captured row-level Source preserving exact CSV columns and values."""

    model_config = ConfigDict(frozen=True)

    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_source(self) -> CSVRowSource:
        expected_id = csv_row_source_id(self.document_id, self.primary_key_value)
        _validate_primary_key_name(self.primary_key)
        _validate_columns_and_values(
            self.columns,
            self.values,
            self.primary_key,
            self.primary_key_value,
        )
        if self.source_id != expected_id or self.name != expected_id:
            raise ValueError("CSVRowSource name and source_id must match csv_row_source_id")
        if self.materialization != SourceMaterialization.CAPTURED:
            raise ValueError("CSVRowSource materialization must be CAPTURED")
        return self


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    """Atomically parse and validate a complete UTF-8 CSV byte document."""

    if not isinstance(value, CSVDocumentInput):
        raise ValueError("value must be a CSVDocumentInput")
    document_id = _validate_document_id(value.document_id)
    primary_key = _validate_primary_key_name(value.primary_key)

    if not isinstance(value.content, bytes) or len(value.content) == 0:
        raise ValueError("CSV content must be non-empty bytes")

    raw_bytes = value.content
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        raw_bytes = raw_bytes[3:]

    try:
        text = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV content must be valid UTF-8") from exc

    if not text:
        raise ValueError("CSV content must not be empty")

    reader = csv.reader(
        io.StringIO(text, newline=""),
        delimiter=",",
        quotechar='"',
        doublequote=True,
        strict=True,
    )

    try:
        header = next(reader, None)
        if header is None or len(header) == 0:
            raise ValueError("CSV header must not be empty")

        columns = tuple(header)
        seen_columns: set[str] = set()
        for col in columns:
            if not col or not col.strip():
                raise ValueError("CSV header fields must be non-empty and not pure whitespace")
            if col in seen_columns:
                raise ValueError(f"Duplicate CSV header column: {col!r}")
            seen_columns.add(col)

        if primary_key not in seen_columns:
            raise ValueError(f"Primary key {primary_key!r} not found in CSV header")
        pk_index = columns.index(primary_key)

        rows: list[CSVRowInput] = []
        seen_pks: set[str] = set()
        width = len(columns)

        for record in reader:
            if len(record) == 0:
                continue
            if len(record) != width:
                raise ValueError(f"CSV row width {len(record)} does not match header width {width}")
            pk_val = record[pk_index]
            _validate_primary_key_value(pk_val)
            if pk_val in seen_pks:
                raise ValueError(f"Duplicate primary key value: {pk_val!r}")
            seen_pks.add(pk_val)
            csv_row_source_id(document_id, pk_val)
            rows.append(
                CSVRowInput(
                    document_id=document_id,
                    primary_key=primary_key,
                    primary_key_value=pk_val,
                    columns=columns,
                    values=tuple(record),
                )
            )
    except csv.Error as exc:
        raise ValueError(f"Malformed CSV content: {exc}") from exc

    return tuple(rows)


class _CSVRowTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row_mapping = dict(zip(source.columns, source.values, strict=True))
        content = json.dumps(row_mapping, ensure_ascii=False, separators=(",", ":"))
        return TextEvidence(
            source_type="csv-row",
            source_id=source.source_id,
            content=content,
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row_mapping,
            },
        )


class _CSVRowSourceDefinition:
    name = "csv-row"
    version = "1"
    input_class = CSVRowInput
    source_class = CSVRowSource
    projections = (_CSVRowTextEvidenceProjection(),)

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        if not isinstance(value, CSVRowInput):
            raise ValueError("value must be CSVRowInput")
        source_id = csv_row_source_id(value.document_id, value.primary_key_value)
        return CSVRowSource(
            name=source_id,
            source_id=source_id,
            definition_version=self.version,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        if not isinstance(source, CSVRowSource):
            raise ValueError("source must be CSVRowSource")
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


CSV_ROW_SOURCE_DEFINITION: SourceDefinition[CSVRowInput, CSVRowSource, CSVRowInput] = _CSVRowSourceDefinition()

__all__ = [
    "CSV_ROW_SOURCE_DEFINITION",
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "csv_row_source_id",
    "parse_csv_document",
]

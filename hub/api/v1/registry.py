import re
from os import getenv
from typing import Annotated, Dict, List, Tuple

import boto3
import botocore
import botocore.exceptions
from dotenv import load_dotenv
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import AfterValidator, BaseModel
from sqlmodel import delete, select, text

from hub.api.v1.auth import AuthToken, revokable_auth
from hub.api.v1.models import RegistryEntry, Tags, get_session

load_dotenv()
S3_BUCKET = getenv("S3_BUCKET")

s3 = boto3.client("s3")

v1_router = APIRouter(
    prefix="/registry",
    tags=["registry"],
)


identifier_pattern = re.compile(r"^[a-zA-Z0-9_\-.]+$")


def valid_identifier(identifier: str) -> str:
    result = identifier_pattern.match(identifier)
    if result is None:
        raise HTTPException(
            status_code=400, detail=f"Invalid identifier: {repr(identifier)}. Should match {identifier_pattern.pattern}"
        )
    return result[0]


tag_pattern = re.compile(r"^[a-zA-Z0-9_\-]+$")


def valid_tag(tag: str) -> bool:
    return tag_pattern.match(tag) is not None


class EntryLocation(BaseModel):
    namespace: Annotated[str, AfterValidator(valid_identifier)]
    name: Annotated[str, AfterValidator(valid_identifier)]
    version: Annotated[str, AfterValidator(valid_identifier)]

    @staticmethod
    def from_str(entry: str) -> "EntryLocation":
        """Create a location from a string in the format namespace/name/version."""
        pattern = re.compile("^(?P<namespace>[^/]+)/(?P<name>[^/]+)/(?P<version>[^/]+)$")
        match = pattern.match(entry)

        if match is None:
            raise ValueError(f"Invalid entry format: {entry}. Should have the format <namespace>/<name>/<version>")

        return EntryLocation(
            namespace=match.group("namespace"),
            name=match.group("name"),
            version=match.group("version"),
        )

    @classmethod
    def as_form(cls, namespace: str = Form(...), name: str = Form(...), version: str = Form(...)):
        """Create a location from form data."""
        return cls(namespace=namespace, name=name, version=version)


def with_write_access(use_forms=False):
    default = Depends(EntryLocation.as_form) if use_forms else Body()

    def fn_with_write_access(
        entry_location: EntryLocation = default,
        auth: AuthToken = Depends(revokable_auth),
    ) -> EntryLocation:
        """Check the user has write access to the entry."""
        if auth.account_id != entry_location.namespace:
            raise HTTPException(
                status_code=403,
                detail=f"Unauthorized. Namespace: {entry_location.namespace} != Account: {auth.account_id}",
            )
        return entry_location

    return fn_with_write_access


def get_or_create(entry_location: EntryLocation = Depends(with_write_access())) -> int:
    with get_session() as session:
        entry = session.exec(
            select(RegistryEntry).where(
                RegistryEntry.namespace == entry_location.namespace,
                RegistryEntry.name == entry_location.name,
                RegistryEntry.version == entry_location.version,
            )
        ).first()

        if entry is None:
            entry = RegistryEntry(
                namespace=entry_location.namespace,
                name=entry_location.name,
                version=entry_location.version,
            )
            session.add(entry)
            session.commit()

        return entry.id


def get(entry_location: EntryLocation = Body()) -> RegistryEntry:
    with get_session() as session:
        entry = session.exec(
            select(RegistryEntry).where(
                RegistryEntry.namespace == entry_location.namespace,
                RegistryEntry.name == entry_location.name,
                RegistryEntry.version == entry_location.version,
            )
        ).first()

        if entry is None:
            raise HTTPException(status_code=404, detail="Entry not found")

        return entry


class EntryMetadataInput(BaseModel):
    category: str
    description: str
    tags: List[str]
    details: Dict
    show_entry: bool


class EntryMetadata(EntryMetadataInput):
    name: str
    version: str


def check_file_exists(key):
    try:
        s3.head_object(Bucket=S3_BUCKET, Key=key)
        return True
    except botocore.exceptions.ClientError as e:
        if e.response["Error"]["Code"] == "404":
            return False
        raise


@v1_router.post("/upload_file")
async def upload_file(
    entry_location: EntryLocation = Depends(with_write_access(use_forms=True)),
    path: str = Form(...),
    file: UploadFile = File(...),
):
    entry = get(entry_location)
    key = entry.get_key(path)

    if check_file_exists(key):
        raise HTTPException(status_code=400, detail=f"File {key} already exists.")

    assert isinstance(S3_BUCKET, str)
    s3.upload_fileobj(file.file, S3_BUCKET, key)

    return {"status": "File uploaded", "path": key}


@v1_router.post("/download_file")
async def download_file(
    entry: RegistryEntry = Depends(get),
    path: str = Body(),
):
    # https://stackoverflow.com/a/71126498/4950797
    assert isinstance(S3_BUCKET, str)
    object = s3.get_object(Bucket=S3_BUCKET, Key=entry.get_key(path))
    return StreamingResponse(object["Body"].iter_chunks())


@v1_router.post("/upload_metadata")
async def upload_metadata(registry_entry_id: int = Depends(get_or_create), metadata: EntryMetadataInput = Body()):
    with get_session() as session:
        entry = session.get(RegistryEntry, registry_entry_id)
        assert entry is not None

        full_metadata = EntryMetadata(name=entry.name, version=entry.version, **metadata.model_dump())

        entry.category = full_metadata.category
        entry.description = full_metadata.description
        entry.details = full_metadata.details
        entry.show_entry = full_metadata.show_entry

        session.add(entry)

        # Delete all previous tags
        # Ignore type check since delete is not supported as a valid statement.
        # https://github.com/tiangolo/sqlmodel/issues/909
        session.exec(delete(Tags).where(Tags.registry_id == entry.id))  # type: ignore

        # Add the new tags
        if len(full_metadata.tags) > 0:
            tags = [Tags(registry_id=entry.id, tag=tag) for tag in full_metadata.tags]
            session.add_all(tags)

        session.commit()

        return {"status": "Updated metadata", "namespace": entry.namespace, "metadata": full_metadata.model_dump()}


@v1_router.post("/download_metadata")
async def download_metadata(entry: RegistryEntry = Depends(get)) -> EntryMetadata:
    with get_session() as session:
        q_tags = select(Tags).where(Tags.registry_id == entry.id)
        tags = [tag.tag for tag in session.exec(q_tags).all()]

    return EntryMetadata(
        name=entry.name,
        version=entry.version,
        category=entry.category,
        description=entry.description,
        tags=tags,
        details=entry.details,
        show_entry=entry.show_entry,
    )


@v1_router.post("/list_files")
async def list_files(entry: RegistryEntry = Depends(get)) -> List[str]:
    """List all files that belong to a entry."""
    key = entry.get_key() + "/"
    assert isinstance(S3_BUCKET, str)
    objects = s3.list_objects(Bucket=S3_BUCKET, Prefix=key)
    files = [obj["Key"][len(key) :] for obj in objects.get("Contents", [])]
    return files


@v1_router.post("/list_entries")
async def list_entries(
    category: str = "",
    tags: str = "",
    total: int = 32,
    show_hidden: bool = False,
) -> List[EntryLocation]:
    # TODO: Return metadata instead of location to show the information in the cli
    # TODO: Return only the latest version of each entry (order by id)

    tags_list = list({tag for tag in tags.split(",") if tag})

    with get_session() as session:
        if len(tags_list) == 0:
            query = select(RegistryEntry)

            if category:
                query = query.where(RegistryEntry.category == category)

            if not show_hidden:
                query = query.where(RegistryEntry.show_entry)

            order_by = RegistryEntry.id.desc()  # type: ignore
            query = query.limit(total).order_by(order_by)

            result = session.exec(query).all()
            return [
                EntryLocation(namespace=entry.namespace, name=entry.name, version=entry.version) for entry in result
            ]
        else:
            assert valid_tag(category)
            assert all(valid_tag(tag) for tag in tags_list)

            tags_input = ",".join(f"'{tag}'" for tag in tags_list)

            query_text = f"""WITH FilteredRegistry AS (
                    SELECT registry.id FROM registryentry registry
                    JOIN tags ON registry.id = tags.registry_id
                    WHERE show_entry >= {1 - int(show_hidden)} AND
                          tags.tag IN ({tags_input}) AND
                          category = '{category}'
                    GROUP BY registry.id
                    HAVING COUNT(DISTINCT tags.tag) = {len(tags_list)}
                    ),
                    RankedRegistry AS (
                        SELECT id, ROW_NUMBER() OVER (ORDER BY id DESC) AS col_rank
                        FROM FilteredRegistry
                    )

                    SELECT registry.id, registry.namespace, registry.name, registry.version,
                           tags.tag FROM RankedRegistry ranked
                    JOIN registryentry registry ON ranked.id = registry.id
                    JOIN tags ON registry.id = tags.registry_id
                    WHERE ranked.col_rank <= {total}
                    ORDER BY registry.id DESC
                """

            result: List[Tuple[int, str, str, str, str]] = session.exec(text(query_text)).all()  # type: ignore
            assert isinstance(result, list)
            filtered = {id: (namespace, name, version) for id, namespace, name, version, _ in result}
            final = sorted(filtered.items(), key=lambda x: x[0], reverse=True)

            return [
                EntryLocation(namespace=namespace, name=name, version=version)
                for _, (namespace, name, version) in final
            ]
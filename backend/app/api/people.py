"""Speaker profiles."""

from __future__ import annotations

from fastapi import APIRouter, File, Query, UploadFile

from ..errors import not_found
from ..schemas import Page, Person, PersonCreate, PersonUpdate, utcnow
from ..store import paginate, store

router = APIRouter(prefix="/people", tags=["people"])


@router.get("", response_model=Page[Person], summary="List people")
def list_people(
    cursor: str | None = Query(default=None, description="Opaque. Pass back `next_cursor`."),
    limit: int = Query(default=50, ge=1, le=200),
) -> Page[Person]:
    items = sorted(store.people.values(), key=lambda p: p.display_name.casefold())
    window, next_cursor = paginate(items, cursor, limit)
    return Page[Person](items=window, next_cursor=next_cursor)


@router.post("", response_model=Person, status_code=201, summary="Create a person")
def create_person(payload: PersonCreate) -> Person:
    now = utcnow()
    person = Person(
        id=store.new_person_id(),
        created_at=now,
        updated_at=now,
        **payload.model_dump(),
    )
    store.people[person.id] = person
    return person


@router.get("/{person_id}", response_model=Person, summary="Get a person")
def get_person(person_id: str) -> Person:
    person = store.people.get(person_id)
    if person is None:
        raise not_found("Person", person_id)
    return person


@router.patch("/{person_id}", response_model=Person, summary="Update a person")
def update_person(person_id: str, payload: PersonUpdate) -> Person:
    person = store.people.get(person_id)
    if person is None:
        raise not_found("Person", person_id)
    updates = payload.model_dump(exclude_unset=True)
    updated = person.model_copy(update={**updates, "updated_at": utcnow()})
    store.people[person_id] = updated
    return updated


@router.delete("/{person_id}", response_model=Person, summary="Delete a person")
def delete_person(person_id: str) -> Person:
    person = store.people.pop(person_id, None)
    if person is None:
        raise not_found("Person", person_id)
    return person


@router.post(
    "/{person_id}/voice-sample",
    response_model=Person,
    summary="Upload a voice sample",
    description=(
        "Reserved for voice-print speaker matching. Milestone 0 records that a sample "
        "exists and does not process the audio."
    ),
)
async def upload_voice_sample(person_id: str, file: UploadFile = File(...)) -> Person:
    person = store.people.get(person_id)
    if person is None:
        raise not_found("Person", person_id)
    await file.read()  # drained and discarded until voice-print matching lands
    updated = person.model_copy(
        update={
            "voice_sample_url": f"/api/people/{person_id}/voice-sample",
            "updated_at": utcnow(),
        }
    )
    store.people[person_id] = updated
    return updated

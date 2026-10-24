import numpy as np
from conftest import textured
from sqlalchemy import func, select

from portaria.db import FACES_VERSION, Embedding, Event, Person, Photo, get_setting
from portaria.decision import Decision
from portaria.events import EventLog, purge_events
from portaria.people import door_allowed, load_gallery


def _person(session, name: str, **fields) -> Person:
    person = Person(name=name, **fields)
    session.add(person)
    session.flush()
    return person


def test_adding_a_photo_stores_the_file_and_one_embedding_per_model(cfg, db, people):
    with db.session() as session:
        ana = _person(session, "Ana")
        check = people.add_photo(session, ana, textured((200, 60, 60)), "upload")
        assert check.ok and check.photo_id
        photo = session.get(Photo, check.photo_id)
        assert (cfg.paths.data_dir / photo.path).is_file()
        assert sorted(e.model for e in photo.embeddings) == ["auraface", "sface"]
        assert get_setting(session, FACES_VERSION) == "1"


def test_avatars_are_a_close_crop_of_the_face_and_go_with_the_photo(cfg, db, people):
    with db.session() as session:
        ana = _person(session, "Ana")
        photo = session.get(Photo, people.add_photo(session, ana, textured((200, 60, 60)), "upload").photo_id)
        crop = people.face_image(photo.path)
        assert crop.name.endswith(".face.jpg") and crop.is_file()
        assert people.face_image(photo.path) == crop  # made once, then reused
        people.delete_photo(session, photo)
        assert not crop.exists()


def test_gallery_has_active_people_only_and_door_access_is_separate(db, people):
    with db.session() as session:
        ana = _person(session, "Ana")
        bia = _person(session, "Bia", door_access=False)
        old = _person(session, "Caio", active=False)
        for person, color in ((ana, (200, 60, 60)), (bia, (60, 200, 60)), (old, (60, 60, 200))):
            people.add_photo(session, person, textured(color), "upload")
        gallery = load_gallery(session, "auraface")
        assert sorted(gallery.names.values()) == ["Ana", "Bia"]
        assert door_allowed(session) == {str(ana.id)}


def test_a_photo_of_someone_already_registered_is_flagged(db, people):
    with db.session() as session:
        ana = _person(session, "Ana")
        people.add_photo(session, ana, textured((200, 60, 60)), "upload")
        check = people.inspect(session, textured((200, 60, 60)))
        assert check.ok and check.looks_like == "Ana"
        assert people.inspect(session, textured((200, 60, 60)), person_id=ana.id).looks_like is None


def test_deleting_a_person_removes_photos_files_and_embeddings_but_keeps_events(cfg, db, people):
    with db.session() as session:
        ana = _person(session, "Ana")
        check = people.add_photo(session, ana, textured((200, 60, 60)), "upload")
        photo_path = cfg.paths.data_dir / session.get(Photo, check.photo_id).path
        ana_id = ana.id
    EventLog(db, cfg.paths.data_dir).record(
        Decision("recognized", str(ana_id), 0.8), textured((0, 0, 0), 64),
        camera_id=None, person_name="Ana", door="shadow", model="auraface", liveness="none",
    )
    with db.session() as session:
        people.delete_person(session, session.get(Person, ana_id))
    with db.session() as session:
        assert not photo_path.exists()
        assert session.scalar(select(func.count()).select_from(Embedding)) == 0
        event = session.scalars(select(Event)).one()
        assert event.person_id is None and event.person_name == "Ana"


def test_old_events_and_their_snapshots_are_purged(cfg, db):
    log = EventLog(db, cfg.paths.data_dir)
    log.record(Decision("unknown", None, 0.1), np.zeros((32, 32, 3), np.uint8),
               camera_id=None, person_name=None, door="kept", model="auraface", liveness="none")
    with db.session() as session:
        event = session.scalars(select(Event)).one()
        snapshot = cfg.paths.data_dir / event.snapshot
        event.at = event.at.replace(year=event.at.year - 1)
    assert snapshot.is_file()
    assert purge_events(db, cfg.paths.data_dir, older_than_days=90) == 1
    assert not snapshot.exists()

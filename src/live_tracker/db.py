"""Database: SQLite through SQLAlchemy, so moving to PostgreSQL later is mostly a connection-string change.

Images stay on disk under the data folder; the database stores their relative paths.
Times are the server's local time, which is what people at the bakery read.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Settings rows bumped whenever something the recognition service has loaded changes.
FACES_VERSION = "faces_version"
CAMERAS_VERSION = "cameras_version"
MODELS_VERSION = "models_version"  # a model was downloaded or removed
DEVICES_VERSION = "devices_version"  # a device or one of its outputs was added, changed or removed
STOP_REQUESTS = "stop_requests"  # bumped when Settings stops the system: the recognition service stops


class Base(DeclarativeBase):
    pass


class Camera(Base):
    __tablename__ = "cameras"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    url: Mapped[str] = mapped_column(String(500))  # RTSP URL (with password), video file, or webcam index
    # What runs on the camera's picture, and what the camera drives. Every camera records with its analysis.
    analysis: Mapped[str] = mapped_column(String(20), default="faces", server_default="faces")  # face recognition
    # The device output it switches when it recognizes someone with access, such as a Tuya module's channel, for
    # that output's pulse time. None: it switches nothing.
    output_id: Mapped[int | None] = mapped_column(ForeignKey("device_outputs.id", ondelete="SET NULL"))
    # "": the whole picture. "top" or "bottom": one lens of a two-lens camera that sends both in one picture.
    part: Mapped[str] = mapped_column(String(10), default="", server_default="")
    # The analysis's own settings, as JSON: for "objects", the kinds of things to record (objects.ObjectSettings).
    model_settings: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    # When to keep a photo of what the model finds (recording.RecordRule): "appear", each time it appears;
    # "interval", at most once every record_every_s, and again while it stays if record_while_present (otherwise
    # only once it leaves and comes back); "daily", the first time each day.
    record_mode: Mapped[str] = mapped_column(String(10), default="appear", server_default="appear")
    record_every_s: Mapped[int] = mapped_column(Integer, default=900, server_default="900")
    record_while_present: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Person(Base):
    """Everyone the system can recognize. The base registry that every module (devices, presence, ...) builds on."""

    __tablename__ = "people"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(12), default="employee")  # "employee", "contractor" or "visitor"
    job_title: Mapped[str] = mapped_column(String(80), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)  # recognized at all
    access: Mapped[bool] = mapped_column(Boolean, default=True)  # recognizing them switches the camera's device
    informed_at: Mapped[datetime | None] = mapped_column(DateTime)  # told about face recognition (LGPD)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    photos: Mapped[list[Photo]] = relationship(
        back_populates="person", cascade="all, delete-orphan", passive_deletes=True, order_by="Photo.id"
    )


class Photo(Base):
    __tablename__ = "photos"

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(String(300))  # relative to the data folder
    source: Mapped[str] = mapped_column(String(10))  # "upload", "camera" or "import"
    face_width: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    person: Mapped[Person] = relationship(back_populates="photos")
    embeddings: Mapped[list[Embedding]] = relationship(
        back_populates="photo", cascade="all, delete-orphan", passive_deletes=True
    )


class Embedding(Base):
    __tablename__ = "embeddings"
    __table_args__ = (UniqueConstraint("photo_id", "model"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    photo_id: Mapped[int] = mapped_column(ForeignKey("photos.id", ondelete="CASCADE"), index=True)
    model: Mapped[str] = mapped_column(String(20))
    vector: Mapped[bytes] = mapped_column(LargeBinary)  # float32, L2-normalized

    photo: Mapped[Photo] = relationship(back_populates="embeddings")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, index=True)
    kind: Mapped[str] = mapped_column(String(12), index=True)  # "recognized", "unknown", "spoof" or "detected"
    # "detected": the kind of thing the people-and-objects model saw (objects.THINGS), and how many at once.
    label: Mapped[str] = mapped_column(String(40), default="", server_default="")
    count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    camera_id: Mapped[int | None] = mapped_column(ForeignKey("cameras.id", ondelete="SET NULL"))
    person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id", ondelete="SET NULL"), index=True)
    person_name: Mapped[str | None] = mapped_column(String(120))  # kept even if the person is deleted
    score: Mapped[float] = mapped_column(Float)
    snapshot: Mapped[str | None] = mapped_column(String(300))  # relative to the data folder
    # What the camera's device did: "switched", "failed" (it didn't switch), "denied" (not someone with access),
    # "shadow" (test mode: only logged); "" at a camera without a device, or for another photo of someone still there
    action: Mapped[str] = mapped_column(String(10))
    model: Mapped[str] = mapped_column(String(20))
    liveness: Mapped[str] = mapped_column(String(20))


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(60), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)  # adds accounts, downloads and deletes records
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)


class AuditEntry(Base):
    """Who changed what: logins, people, photos, cameras. What you need to answer an LGPD request."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    username: Mapped[str] = mapped_column(String(60))
    action: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str] = mapped_column(String(300), default="")


class Device(Base):
    """A board or module: active, with outputs that switch things (relays), and passive, with inputs (sensors).

    "esp32": a board with our firmware, over MQTT (firmware/README.md); it announces itself. "tuya": a ready-made
    Wi-Fi module made by Tuya (NovaDigital and others), on the local network with its local key; imported from the
    Tuya developer account. The recognition service keeps the connections (devices/).
    """

    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(10))  # "esp32" or "tuya"
    uid: Mapped[str] = mapped_column(String(64), unique=True)  # the board's device ID, or the Tuya device ID
    address: Mapped[str] = mapped_column(String(64), default="")  # a fixed IP; "": found on the network
    local_key: Mapped[str] = mapped_column(String(64), default="")  # Tuya only; never shown or logged
    model: Mapped[str] = mapped_column(String(80), default="")  # Tuya's model, or the board's firmware version
    new: Mapped[bool] = mapped_column(Boolean, default=False)  # a board that announced itself, not named yet
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    outputs: Mapped[list[DeviceOutput]] = relationship(
        back_populates="device", cascade="all, delete-orphan", passive_deletes=True, order_by="DeviceOutput.id"
    )
    inputs: Mapped[list[DeviceInput]] = relationship(
        back_populates="device", cascade="all, delete-orphan", passive_deletes=True, order_by="DeviceInput.id"
    )


class DeviceOutput(Base):
    """A relay. Every pulse ends on the device itself, after at most max_ms, even when the network is down."""

    __tablename__ = "device_outputs"
    __table_args__ = (UniqueConstraint("device_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(20))  # the board's output key, such as "relay1"; Tuya: its switch's DP
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(10), default="relay")  # "lock", "light", "siren" or "relay"
    pulse_ms: Mapped[int] = mapped_column(Integer, default=3000)  # how long it stays on when a rule switches it on
    max_ms: Mapped[int] = mapped_column(Integer, default=10000)  # the longest the device keeps it on
    timer_dp: Mapped[int | None] = mapped_column(Integer)  # Tuya: the countdown that switches it off on the module

    device: Mapped[Device] = relationship(back_populates="outputs")


class DeviceInput(Base):
    """A button or a sensor on a board."""

    __tablename__ = "device_inputs"
    __table_args__ = (UniqueConstraint("device_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(10), default="button")  # "button" or "contact"

    device: Mapped[Device] = relationship(back_populates="inputs")


class DeviceCommand(Base):
    """A command for an output, and what came of it. The web UI adds one; the recognition service, which holds the
    connections, carries it out and fills in the result. One not picked up within seconds is dropped, never sent
    late (devices.LATE_S)."""

    __tablename__ = "device_commands"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    output_key: Mapped[str] = mapped_column(String(20))
    action: Mapped[str] = mapped_column(String(10), default="pulse")
    ms: Mapped[int] = mapped_column(Integer)
    requested_by: Mapped[str] = mapped_column(String(60))  # who pressed Test
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    done_at: Mapped[datetime | None] = mapped_column(DateTime)
    # "": waiting; "sent": being carried out; "ok"; "failed" (error says why); "late": dropped, never sent
    result: Mapped[str] = mapped_column(String(10), default="", index=True)
    error: Mapped[str] = mapped_column(String(200), default="")


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.url = f"sqlite:///{path.resolve().as_posix()}"
        self.engine = create_engine(self.url, connect_args={"check_same_thread": False})
        event.listen(self.engine, "connect", _sqlite_pragmas)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    def migrate(self) -> None:
        """Create or upgrade the tables to the current version."""
        logging.getLogger("alembic").setLevel(logging.WARNING)  # before importing it: it logs while loading
        from alembic import command
        from alembic.config import Config as AlembicConfig

        alembic_cfg = AlembicConfig()
        alembic_cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
        alembic_cfg.set_main_option("sqlalchemy.url", self.url)
        command.upgrade(alembic_cfg, "head")

    @contextmanager
    def session(self) -> Iterator[Session]:
        """A session in a transaction: committed when the block ends, rolled back on errors."""
        with self._sessions() as session, session.begin():
            yield session


def _sqlite_pragmas(connection, _record) -> None:
    cursor = connection.cursor()
    # WAL lets the web UI read while the recognition service writes; busy_timeout waits instead of failing.
    for pragma in ("journal_mode=WAL", "foreign_keys=ON", "busy_timeout=5000", "synchronous=NORMAL"):
        cursor.execute(f"PRAGMA {pragma}")
    cursor.close()


def get_setting(session: Session, key: str, default: str | None = None) -> str | None:
    row = session.get(Setting, key)
    return row.value if row else default


def set_setting(session: Session, key: str, value: str) -> None:
    row = session.get(Setting, key)
    if row:
        row.value = value
    else:
        session.add(Setting(key=key, value=value))


def ensure_setting(session: Session, key: str, create: Callable[[], str]) -> str:
    """The setting's value, creating it on first use (secrets, tokens)."""
    value = get_setting(session, key)
    if value is None:
        value = create()
        set_setting(session, key, value)
    return value


def bump(session: Session, key: str) -> None:
    """Mark something as changed, so the recognition service reloads it."""
    set_setting(session, key, str(int(get_setting(session, key, "0") or 0) + 1))


def audit(session: Session, username: str, action: str, detail: str = "") -> None:
    session.add(AuditEntry(username=username, action=action, detail=detail[:300]))

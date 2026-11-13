"""Exercise the release migration against an ephemeral PostgreSQL + pgvector database."""

import os
import unittest
from pathlib import Path

import psycopg
from psycopg import errors
from psycopg.types.json import Jsonb


ROOT = Path(__file__).resolve().parents[2]
LEGACY = "r1-" + "1" * 20
NEXT = "r1-" + "2" * 20
STALE = "r1-" + "3" * 20
VIDEO = {"video_id": "synthetic-video", "title": "Synthetic video"}
VECTOR = "[1," + "0," * 1534 + "0]"


def connect(user):
    return psycopg.connect(
        host="127.0.0.1",
        port=os.environ["TOTALITY_CI_PG_PORT"],
        dbname="totality_ci",
        user=user,
        password="ci-only-password",
        autocommit=True,
        connect_timeout=5,
    )


def manifest(release_id, base=None):
    return Jsonb({"release_id": release_id, "base_release": base, "videos": 1, "chunks": 1})


def legacy_state(db):
    return db.execute(
        """SELECT a.release_id, r.manifest, r.created_at, c.payload, c.embedding::text
           FROM rag.active_release a
           JOIN rag.releases r USING (release_id)
           JOIN rag.chunks c USING (release_id)"""
    ).fetchone()


def stage(db, release_id):
    db.execute("SELECT rag.stage_release(%s, %s)", (manifest(release_id, LEGACY), Jsonb([VIDEO])))
    payload = Jsonb({
        "chunk_id": release_id + "-chunk",
        "release_id": release_id,
        "video_id": VIDEO["video_id"],
        "content_sha256": "a" * 64,
        "text": "Synthetic test passage",
    })
    db.execute("SELECT rag.put_chunk(%s, %s, %s::vector)", (payload, "synthetic", VECTOR))
    db.execute("SELECT rag.seal_release(%s)", (release_id,))


class PostgresReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        migration = (ROOT / "deploy/migrate-release-v2.sql").read_text(encoding="utf-8")
        with connect("postgres") as db:
            db.execute((ROOT / "tests/fixtures/schema-v1.sql").read_text(encoding="utf-8"))
            db.execute("CREATE ROLE rag_runtime LOGIN PASSWORD 'ci-only-password' IN ROLE rag_reader")
            db.execute(
                "INSERT INTO rag.releases (release_id, manifest) VALUES (%s, %s)",
                (LEGACY, manifest(LEGACY)),
            )
            db.execute(
                """INSERT INTO rag.chunks
                   (chunk_id, release_id, video_id, content_sha256, model, dimensions,
                    payload, keywords, embedding)
                   VALUES ('legacy-chunk', %s, %s, %s, 'text-embedding-3-small', 1536,
                           %s, to_tsvector('simple', 'synthetic'), %s::vector)""",
                (LEGACY, VIDEO["video_id"], "b" * 64, Jsonb({"text": "Synthetic legacy"}), VECTOR),
            )
            db.execute("INSERT INTO rag.active_release VALUES (true, %s)", (LEGACY,))
            cls.original = legacy_state(db)

            db.execute(migration)
            cls.first_migration = legacy_state(db)
            cls.initial_metadata = db.execute(
                "SELECT videos, sealed FROM rag.releases WHERE release_id = %s", (LEGACY,)
            ).fetchone()
            # Simulate the metadata backfill between runs to detect a destructive rerun.
            db.execute("UPDATE rag.releases SET videos = %s WHERE release_id = %s", (Jsonb([VIDEO]), LEGACY))
            db.execute(migration)
            cls.second_migration = legacy_state(db)
            cls.rerun_metadata = db.execute(
                "SELECT videos, sealed FROM rag.releases WHERE release_id = %s", (LEGACY,)
            ).fetchone()
            db.execute("CREATE ROLE rag_ingest LOGIN PASSWORD 'ci-only-password' IN ROLE rag_loader")

    def test_legacy_migration_can_be_rerun(self):
        self.assertIsNotNone(self.original)
        self.assertEqual(self.first_migration, self.original)
        self.assertEqual(self.initial_metadata, (None, True))
        self.assertEqual(self.second_migration, self.original)
        self.assertEqual(self.rerun_metadata, ([VIDEO], True))

    def test_reader_and_loader_privileges(self):
        with connect("rag_runtime") as reader:
            self.assertEqual(reader.execute("SHOW transaction_read_only").fetchone(), ("on",))
            self.assertEqual(reader.execute(
                "SELECT vector_dims(embedding) FROM rag.chunks WHERE chunk_id = 'legacy-chunk'"
            ).fetchone(), (1536,))
            # Check ACLs independently of the reader session's default read-only setting.
            reader.execute("SET default_transaction_read_only = off")
            for statement, params in (
                ("UPDATE rag.active_release SET release_id = %s", (LEGACY,)),
                ("SELECT rag.stage_release(%s, %s)", (manifest(NEXT, LEGACY), Jsonb([VIDEO]))),
                ("SELECT rag.activate_release(%s, %s)", (NEXT, LEGACY)),
            ):
                with self.subTest(role="reader", statement=statement):
                    with self.assertRaises(errors.InsufficientPrivilege):
                        reader.execute(statement, params)

        with connect("rag_ingest") as loader:
            for statement in (
                "INSERT INTO rag.releases (release_id, manifest) VALUES ('direct-write', '{}')",
                "UPDATE rag.active_release SET release_id = release_id",
                "DELETE FROM rag.chunks WHERE chunk_id = 'legacy-chunk'",
                "CREATE TABLE rag.unexpected (id integer)",
                "ALTER TABLE rag.releases ADD COLUMN unexpected integer",
            ):
                with self.subTest(role="loader", statement=statement):
                    with self.assertRaises(errors.InsufficientPrivilege):
                        loader.execute(statement)

    def test_activation_rejects_stale_base(self):
        with connect("rag_ingest") as loader:
            stage(loader, NEXT)
            stage(loader, STALE)
            loader.execute("SELECT rag.activate_release(%s, %s)", (NEXT, LEGACY))

            with self.assertRaisesRegex(errors.RaiseException, "CAS base mismatch"):
                loader.execute("SELECT rag.activate_release(%s, %s)", (STALE, LEGACY))
            with self.assertRaisesRegex(errors.RaiseException, "candidate base mismatch"):
                loader.execute("SELECT rag.activate_release(%s, %s)", (STALE, NEXT))

        with connect("rag_runtime") as reader:
            self.assertEqual(reader.execute("SELECT release_id FROM rag.active_release").fetchall(), [(NEXT,)])

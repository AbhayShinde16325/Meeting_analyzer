"""Add voice speaker profiles, embeddings, and assignments.

Voice Profiling V1:

* ``speaker_profiles`` — persistent people, named only by users.
* ``speaker_embeddings`` — one voice embedding per (transcript, local
  diarization label); linked to a profile only after user confirmation.
* ``speaker_assignments`` — user-reviewable mapping from a meeting-local
  speaker label to a profile with suggested/confirmed/rejected state.
* ``processing_jobs.job_type`` gains the ``'profiling'`` value.

``0001_initial`` bootstraps the schema with ``Base.metadata.create_all``,
which already creates these tables on a *fresh* database (the models exist
by then).  The creates below are therefore checkfirst: they apply cleanly
both on databases stamped at ``0001_initial`` (pre-speaker tables) and on
fresh ones.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Uuid, text
from sqlalchemy.dialects.postgresql import JSONB

import db.models  # noqa: F401  (populates Base.metadata)
from db.base import Base

revision = "0002_speaker_profiles"
down_revision = "0001_initial"

_TABLES = ("speaker_profiles", "speaker_embeddings", "speaker_assignments")

_JOB_TYPE_CHECK = "ck_processing_jobs_job_type_allowed"


def _speaker_tables():
    return [Base.metadata.tables[name] for name in _TABLES]


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind, tables=_speaker_tables(), checkfirst=True)

    op.execute(
        f"ALTER TABLE processing_jobs DROP CONSTRAINT IF EXISTS {_JOB_TYPE_CHECK}"
    )
    op.execute(
        f"ALTER TABLE processing_jobs ADD CONSTRAINT {_JOB_TYPE_CHECK} "
        "CHECK (job_type IN ('transcription', 'analysis', 'profiling'))"
    )


def downgrade() -> None:
    op.execute(
        f"ALTER TABLE processing_jobs DROP CONSTRAINT IF EXISTS {_JOB_TYPE_CHECK}"
    )
    op.execute(
        f"ALTER TABLE processing_jobs ADD CONSTRAINT {_JOB_TYPE_CHECK} "
        "CHECK (job_type IN ('transcription', 'analysis'))"
    )
    for name in reversed(_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {name}")

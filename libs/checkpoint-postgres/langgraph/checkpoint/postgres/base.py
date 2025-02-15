from base64 import b64decode, b64encode
from hashlib import md5
from typing import Any, List, Optional, Tuple

from langchain_core.runnables import RunnableConfig
from psycopg.types.json import Jsonb

from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    Checkpoint,
    EmptyChannelError,
    get_checkpoint_id,
)
from langgraph.checkpoint.serde.types import ChannelProtocol

MetadataInput = Optional[dict[str, Any]]

SELECT_SQL = """
select
    thread_id,
    checkpoint,
    checkpoint_ns,
    checkpoint_id,
    parent_checkpoint_id,
    metadata,
    (
        select array_agg(array[bl.channel::bytea, bl.type::bytea, bl.blob])
        from jsonb_each_text(checkpoint -> 'channel_versions')
        inner join checkpoint_blobs bl
            on bl.thread_id = checkpoints.thread_id
            and bl.checkpoint_ns = checkpoints.checkpoint_ns
            and bl.channel = jsonb_each_text.key
            and bl.version = jsonb_each_text.value
    ) as channel_values,
    (
        select
        array_agg(array[cw.task_id::text::bytea, cw.channel::bytea, cw.type::bytea, cw.blob])
        from checkpoint_writes cw
        where cw.thread_id = checkpoints.thread_id
            and cw.checkpoint_ns = checkpoints.checkpoint_ns
            and cw.checkpoint_id = checkpoints.checkpoint_id
    ) as pending_writes
from checkpoints """

CREATE_CHECKPOINTS_SQL = """
    CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    type TEXT,
    checkpoint JSONB NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);
"""
CREATE_CHECKPOINT_BLOBS_SQL = """
    CREATE TABLE IF NOT EXISTS checkpoint_blobs (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL,
    version TEXT NOT NULL,
    type TEXT NOT NULL,
    blob BYTEA NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);"""

CREATE_CHECKPOINT_WRITES_SQL = """
    CREATE TABLE IF NOT EXISTS checkpoint_writes (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    channel TEXT NOT NULL,
    type TEXT,
    blob BYTEA NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);
"""

UPSERT_CHECKPOINT_BLOBS_SQL = """
    INSERT INTO checkpoint_blobs (thread_id, checkpoint_ns, channel, version, type, blob)
    VALUES (%s, %s, %s, %s, %s, %s)
    ON CONFLICT (thread_id, checkpoint_ns, channel, version) DO NOTHING
"""

UPSERT_CHECKPOINTS_SQL = """
    INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, checkpoint, metadata)
    VALUES (%s, %s, %s, %s, %s, %s)
    ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id)
    DO UPDATE SET
        checkpoint = EXCLUDED.checkpoint,
        metadata = EXCLUDED.metadata;
"""

UPSERT_CHECKPOINT_WRITES_SQL = """
    INSERT INTO checkpoint_writes (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, blob)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id, task_id, idx) DO NOTHING
"""


class BasePostgresSaver(BaseCheckpointSaver):
    SELECT_SQL = SELECT_SQL
    CREATE_CHECKPOINTS_SQL = CREATE_CHECKPOINTS_SQL
    CREATE_CHECKPOINT_BLOBS_SQL = CREATE_CHECKPOINT_BLOBS_SQL
    CREATE_CHECKPOINT_WRITES_SQL = CREATE_CHECKPOINT_WRITES_SQL
    UPSERT_CHECKPOINT_BLOBS_SQL = UPSERT_CHECKPOINT_BLOBS_SQL
    UPSERT_CHECKPOINTS_SQL = UPSERT_CHECKPOINTS_SQL
    UPSERT_CHECKPOINT_WRITES_SQL = UPSERT_CHECKPOINT_WRITES_SQL

    def _load_checkpoint(self, checkpoint: dict[str, Any]) -> Checkpoint:
        if len(checkpoint["pending_sends"]) == 2 and all(
            isinstance(a, str) for a in checkpoint["pending_sends"]
        ):
            type, bs = checkpoint["pending_sends"]
            return {
                **checkpoint,
                "pending_sends": self.serde.loads_typed((type, b64decode(bs))),
            }

        return checkpoint

    def _dump_checkpoint(self, checkpoint: Checkpoint) -> dict[str, Any]:
        type, bs = self.serde.dumps_typed(checkpoint["pending_sends"])
        return {
            **checkpoint,
            "pending_sends": (type, b64encode(bs).decode()),
        }

    def _load_blobs(
        self, blob_values: list[tuple[bytes, bytes, bytes]]
    ) -> dict[str, Any]:
        if not blob_values:
            return {}
        return {
            k.decode(): self.serde.loads_typed((t.decode(), v))
            for k, t, v in blob_values
        }

    def _dump_blobs(
        self,
        thread_id: str,
        checkpoint_ns: str,
        values: dict[str, Any],
        versions: dict[str, str],
        new_versions: Optional[dict[str, str]],
    ) -> list[tuple[str, str, str, str, str, bytes]]:
        if not versions:
            return []

        if new_versions:
            versions = new_versions

        return [
            (
                thread_id,
                checkpoint_ns,
                k,
                ver,
                *self.serde.dumps_typed(values[k]),
            )
            for k, ver in versions.items()
            if k in values
        ]

    def _load_writes(
        self, writes: list[tuple[bytes, bytes, bytes, bytes]]
    ) -> list[tuple[str, str, Any]]:
        return (
            [
                (
                    tid.decode(),
                    channel.decode(),
                    self.serde.loads_typed((t.decode(), v)),
                )
                for tid, channel, t, v in writes
            ]
            if writes
            else []
        )

    def _dump_writes(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
        task_id: str,
        writes: list[tuple[str, Any]],
    ) -> list[tuple[str, str, str, int, str, str, bytes]]:
        return [
            (
                thread_id,
                checkpoint_ns,
                checkpoint_id,
                task_id,
                idx,
                channel,
                *self.serde.dumps_typed(value),
            )
            for idx, (channel, value) in enumerate(writes)
        ]

    def get_next_version(self, current: Optional[str], channel: ChannelProtocol) -> str:
        if current is None:
            current_v = 0
        elif isinstance(current, int):
            current_v = current
        else:
            current_v = int(current.split(".")[0])
        next_v = current_v + 1
        try:
            next_h = md5(self.serde.dumps_typed(channel.checkpoint())[1]).hexdigest()
        except EmptyChannelError:
            next_h = ""
        return f"{next_v:032}.{next_h}"

    def _search_where(
        self,
        config: Optional[RunnableConfig],
        filter: MetadataInput,
        before: Optional[RunnableConfig] = None,
    ) -> Tuple[str, List[Any]]:
        """Return WHERE clause predicates for alist() given config, filter, cursor.

        This method returns a tuple of a string and a tuple of values. The string
        is the parametered WHERE clause predicate (including the WHERE keyword):
        "WHERE column1 = $1 AND column2 IS $2". The list of values contains the
        values for each of the corresponding parameters.
        """
        wheres = []
        param_values = []

        # construct predicate for config filter
        if config:
            wheres.append("thread_id = %s ")
            param_values.append(config["configurable"]["thread_id"])
            checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
            wheres.append("checkpoint_ns = %s")
            param_values.append(checkpoint_ns)

        # construct predicate for metadata filter
        if filter:
            wheres.append("metadata @> %s ")
            param_values.append(Jsonb(filter))

        # construct predicate for `before`
        if before is not None:
            wheres.append("checkpoint_id < %s ")
            param_values.append(get_checkpoint_id(before))

        return (
            "WHERE " + " AND ".join(wheres) if wheres else "",
            param_values,
        )

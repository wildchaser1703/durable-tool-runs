"""Frozen validation of the 0001 schema, independent of evolving ORM models."""

import sqlalchemy as sa


def validate_baseline(connection):
    inspector = sa.inspect(connection)
    required = {
        "runs": {
            "id": (sa.String(36), False),
            "status": (sa.String(32), False),
            "cancellation_requested": (sa.Boolean(), False),
        },
        "steps": {
            "id": (sa.String(36), False),
            "run_id": (sa.String(36), False),
            "position": (sa.Integer(), False),
            "tool_name": (sa.String(80), False),
            "payload": (sa.JSON(), False),
            "idempotency_key": (sa.String(128), False),
            "status": (sa.String(32), False),
            "attempts": (sa.Integer(), False),
            "lease_owner": (sa.String(80), True),
            "lease_expires_at": (sa.DateTime(timezone=True), True),
            "result": (sa.JSON(), True),
            "error": (sa.Text(), True),
        },
        "run_events": {
            "id": (sa.Integer(), False),
            "run_id": (sa.String(36), False),
            "step_id": (sa.String(36), True),
            "kind": (sa.String(80), False),
            "detail": (sa.JSON(), False),
        },
    }
    for table, columns in required.items():
        columns["created_at"] = (sa.DateTime(timezone=True), False)
        if table == "runs":
            columns["updated_at"] = (sa.DateTime(timezone=True), False)
        if table not in inspector.get_table_names():
            raise RuntimeError(
                f"Baseline schema mismatch: missing {table}; restore or repair before migrating"
            )
        actual = {column["name"]: column for column in inspector.get_columns(table)}
        if actual.keys() != columns.keys():
            raise RuntimeError(f"Baseline schema mismatch: columns in {table}")
        for name, (expected_type, nullable) in columns.items():
            column = actual[name]
            if (
                str(column["type"].compile(dialect=connection.dialect))
                != str(expected_type.compile(dialect=connection.dialect))
                or column["nullable"] != nullable
            ):
                raise RuntimeError(
                    f"Baseline schema mismatch: type/nullability of {table}.{name}"
                )
        if inspector.get_pk_constraint(table)["constrained_columns"] != ["id"]:
            raise RuntimeError(f"Baseline schema mismatch: primary key in {table}")
    unique = {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints("steps")
    }
    if not {("idempotency_key",), ("run_id", "position")} <= unique:
        raise RuntimeError("Baseline schema mismatch: step uniqueness constraints")
    for table, column, target, action in [
        ("steps", "run_id", "runs", "CASCADE"),
        ("run_events", "run_id", "runs", "CASCADE"),
        ("run_events", "step_id", "steps", "SET NULL"),
    ]:
        if not any(
            f["constrained_columns"] == [column]
            and f["referred_table"] == target
            and f["referred_columns"] == ["id"]
            and f["options"].get("ondelete") == action
            for f in inspector.get_foreign_keys(table)
        ):
            raise RuntimeError(
                f"Baseline schema mismatch: foreign key {table}.{column}"
            )
    for table, expected in [
        ("runs", {("status",)}),
        ("steps", {("run_id",), ("status",), ("lease_expires_at",)}),
        ("run_events", {("run_id",)}),
    ]:
        if not expected <= {
            tuple(i["column_names"]) for i in inspector.get_indexes(table)
        }:
            raise RuntimeError(f"Baseline schema mismatch: indexes in {table}")

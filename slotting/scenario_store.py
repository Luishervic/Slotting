"""Persistencia local e inmutable de escenarios en SQLite."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd


SCHEMA = """
CREATE TABLE IF NOT EXISTS scenario (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    name TEXT NOT NULL,
    facility TEXT NOT NULL DEFAULT 'GENERAL',
    zone TEXT NOT NULL,
    parent_id TEXT,
    source_file TEXT,
    source_version TEXT,
    parameters_json TEXT NOT NULL,
    kpis_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scenario_artifact (
    scenario_id TEXT NOT NULL,
    name TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (scenario_id, name),
    FOREIGN KEY (scenario_id) REFERENCES scenario(id)
);
"""


def _json_default(value):
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if np.isnan(value) else float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if pd.isna(value):
        return None
    raise TypeError(f"No se puede serializar {type(value).__name__}")


def _dumps(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, default=_json_default
    )


def _artifact_payload(value) -> dict:
    if isinstance(value, pd.DataFrame):
        limpio = value.copy()
        limpio = limpio.where(pd.notna(limpio), None)
        return {
            "kind": "dataframe",
            "columns": list(limpio.columns),
            "records": limpio.to_dict("records"),
        }
    return {"kind": "json", "value": value}


class ScenarioStore:
    """Repositorio local. Guardar crea una nueva versión; nunca sobrescribe."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    @contextmanager
    def _connect(self):
        con = sqlite3.connect(self.path)
        con.execute("PRAGMA foreign_keys = ON")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def _init(self) -> None:
        with self._connect() as con:
            con.executescript(SCHEMA)
            columnas = {
                fila[1] for fila in con.execute(
                    "PRAGMA table_info(scenario)"
                ).fetchall()
            }
            if "facility" not in columnas:
                con.execute(
                    "ALTER TABLE scenario ADD COLUMN facility TEXT "
                    "NOT NULL DEFAULT 'GENERAL'"
                )
            con.execute(
                "CREATE INDEX IF NOT EXISTS "
                "idx_scenario_facility_zone_created "
                "ON scenario(facility, zone, created_at DESC)"
            )

    def save(
        self,
        *,
        name: str,
        zone: str,
        facility: str = "GENERAL",
        parameters: dict,
        kpis: dict,
        artifacts: dict | None = None,
        parent_id: str | None = None,
        source_file: str | None = None,
        source_version: str | None = None,
    ) -> str:
        facility_code = "".join(
            c for c in str(facility).strip().upper() if c.isalnum()
        )[:12] or "GENERAL"
        zone_code = "".join(
            c for c in str(zone).strip().upper() if c.isalnum()
        )[:12] or "MIXTA"
        scenario_id = (
            f"ESC-{facility_code}-{zone_code}-"
            f"{uuid.uuid4().hex[:10].upper()}"
        )
        created = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO scenario
                (id, created_at, name, facility, zone, parent_id, source_file,
                 source_version, parameters_json, kpis_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scenario_id,
                    created,
                    str(name).strip() or scenario_id,
                    str(facility).strip().upper() or "GENERAL",
                    str(zone).strip().upper(),
                    parent_id,
                    source_file,
                    source_version,
                    _dumps(parameters),
                    _dumps(kpis),
                ),
            )
            for artifact_name, value in (artifacts or {}).items():
                con.execute(
                    """
                    INSERT INTO scenario_artifact
                    (scenario_id, name, payload_json) VALUES (?, ?, ?)
                    """,
                    (
                        scenario_id,
                        str(artifact_name),
                        _dumps(_artifact_payload(value)),
                    ),
                )
        return scenario_id

    def list(
        self,
        zone: str | None = None,
        facility: str | None = None,
    ) -> pd.DataFrame:
        sql = (
            "SELECT id, created_at, name, facility, zone, parent_id, source_file, "
            "source_version, parameters_json, kpis_json FROM scenario"
        )
        condiciones, valores = [], []
        if zone:
            condiciones.append("zone = ?")
            valores.append(str(zone).strip().upper())
        if facility:
            condiciones.append("facility = ?")
            valores.append(str(facility).strip().upper())
        if condiciones:
            sql += " WHERE " + " AND ".join(condiciones)
        sql += " ORDER BY created_at DESC"
        with self._connect() as con:
            rows = pd.read_sql_query(sql, con, params=tuple(valores))
        if rows.empty:
            return rows
        rows["parameters"] = rows.pop("parameters_json").map(json.loads)
        rows["kpis"] = rows.pop("kpis_json").map(json.loads)
        return rows

    def artifact(self, scenario_id: str, name: str):
        with self._connect() as con:
            row = con.execute(
                """
                SELECT payload_json FROM scenario_artifact
                WHERE scenario_id = ? AND name = ?
                """,
                (scenario_id, name),
            ).fetchone()
        if row is None:
            raise KeyError(f"El escenario {scenario_id} no tiene artefacto {name}")
        payload = json.loads(row[0])
        if payload["kind"] == "dataframe":
            return pd.DataFrame(
                payload["records"], columns=payload.get("columns")
            )
        return payload.get("value")

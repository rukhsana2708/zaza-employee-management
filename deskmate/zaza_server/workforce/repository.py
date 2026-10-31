"""PostgreSQL workforce persistence; obtained only inside a resolved tenant lease.

This is an internal persistence adapter, not an authorization boundary.
"""

import json
import uuid
from contextlib import contextmanager

from psycopg import DataError, IntegrityError, sql

from ..postgres import migrate
from ..tenancy.domain import TenancyError

CATALOGS = {"designation": "designations", "department": "departments", "team": "teams"}


class WorkforceRepository:
    def __init__(self, conn, actor_id):
        self.conn, self.actor_id = conn, actor_id

    @classmethod
    @contextmanager
    def transaction(cls, repo, actor_id):
        with repo.connection() as conn:
            if not migrate.schema_status(conn).up_to_date:
                raise TenancyError("workforce schema migration is pending")
            try:
                with conn.transaction():
                    # A small V1 administrative domain: serialize graph/link/profile edits.
                    conn.execute("SELECT pg_advisory_xact_lock(1514234449)")
                    yield cls(conn, actor_id)
            except (IntegrityError, DataError):
                raise TenancyError("the change violates a workforce constraint") from None

    @classmethod
    @contextmanager
    def read_transaction(cls, repo, actor_id):
        """Lock before snapshot so completed scope revocations cannot be read from an old snapshot."""
        with repo.connection() as conn:
            conn.execute("SELECT pg_advisory_lock_shared(1514234449)")
            try:
                with conn.transaction():
                    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    if not migrate.schema_status(conn).up_to_date:
                        raise TenancyError("workforce schema migration is pending")
                    yield cls(conn, actor_id)
            finally:
                try:
                    conn.execute("SELECT pg_advisory_unlock_shared(1514234449)")
                except BaseException:
                    conn.close()  # never return a pooled connection with a leaked session lock
                    raise

    def employee_ids(self):
        return [
            r["employee_id"]
            for r in self.conn.execute("SELECT employee_id FROM employees ORDER BY employee_id")
        ]

    def direct_report_ids(self, manager_id, now):
        return [
            r["employee_id"]
            for r in self.conn.execute(
                "SELECT employee_id FROM employee_reporting_relationships WHERE manager_employee_id=%s "
                "AND effective_from<=%s AND (effective_to IS NULL OR effective_to>%s)",
                (manager_id, now, now),
            )
        ]

    def audit(self, action, entity, identity, old=None, new=None):
        self.conn.execute(
            "INSERT INTO audit_logs "
            "(actor_type,actor_id,action,entity_type,entity_id,old_values,new_values) "
            "VALUES ('SAAS_USER',%s,%s,%s,%s,%s::jsonb,%s::jsonb)",
            (
                str(self.actor_id),
                action,
                entity,
                str(identity),
                json.dumps(old, default=str) if old is not None else None,
                json.dumps(new, default=str) if new is not None else None,
            ),
        )

    def profile(self, employee_id):
        row = self.conn.execute(
            "SELECT e.employee_id,e.display_name,e.role,e.is_active,e.timezone,p.* "
            "FROM employees e JOIN employee_profiles p USING(employee_id) WHERE employee_id=%s",
            (employee_id,),
        ).fetchone()
        if row is None:
            raise TenancyError("employee not found")
        return row

    def catalog(self, kind, identity):
        table = CATALOGS.get(kind)
        if table is None:
            raise TenancyError("unknown catalog")
        row = self.conn.execute(
            sql.SQL("SELECT * FROM {} WHERE id=%s").format(sql.Identifier(table)), (identity,)
        ).fetchone()
        if row is None:
            raise TenancyError("catalog entry not found")
        return row

    def create_catalog(self, kind, name):
        table = CATALOGS.get(kind)
        if table is None:
            raise TenancyError("unknown catalog")
        row = self.conn.execute(
            sql.SQL("INSERT INTO {} (id,display_name) VALUES (%s,%s) RETURNING *").format(
                sql.Identifier(table)
            ),
            (uuid.uuid4(), name),
        ).fetchone()
        self.audit(f"{kind}.create", kind, row["id"], new=row)
        return row

    def update_catalog(self, kind, identity, version, name, active):
        old = self.catalog(kind, identity)
        row = self.conn.execute(
            sql.SQL(
                "UPDATE {} SET display_name=%s,is_active=%s,version=version+1,"
                "updated_at=clock_timestamp() WHERE id=%s AND version=%s RETURNING *"
            ).format(sql.Identifier(CATALOGS[kind])),
            (name, active, identity, version),
        ).fetchone()
        if row is None:
            raise TenancyError("catalog version conflict")
        self.audit(f"{kind}.update", kind, identity, old, row)
        return row

    def update_profile(self, employee_id, version, changes):
        old = self.profile(employee_id)
        allowed = {"designation_id", "department_id", "employment_status", "monitoring_enabled"}
        if not changes or not set(changes) <= allowed:
            raise TenancyError("unsupported profile fields")
        sets = sql.SQL(",").join(sql.SQL("{}=%s").format(sql.Identifier(k)) for k in changes)
        row = self.conn.execute(
            sql.SQL(
                "UPDATE employee_profiles SET {},version=version+1,"
                "updated_at=clock_timestamp() WHERE employee_id=%s AND version=%s RETURNING *"
            ).format(sets),
            (*changes.values(), employee_id, version),
        ).fetchone()
        if row is None:
            raise TenancyError("profile version conflict")
        self.audit(
            "employee.update",
            "employee_profile",
            employee_id,
            {k: old[k] for k in changes},
            changes,
        )
        return self.profile(employee_id)

    def link_for_user(self, user_id, membership_id):
        return self.conn.execute(
            "SELECT * FROM employee_account_links WHERE control_user_id=%s "
            "AND control_membership_id=%s AND revoked_at IS NULL",
            (user_id, membership_id),
        ).fetchone()

    def link(self, employee_id, user_id, membership_id, replace=False):
        self.profile(employee_id)
        if replace:
            self.revoke_links(employee_id)
        row = self.conn.execute(
            "INSERT INTO employee_account_links "
            "(id,employee_id,control_user_id,control_membership_id) VALUES (%s,%s,%s,%s) RETURNING *",
            (uuid.uuid4(), employee_id, user_id, membership_id),
        ).fetchone()
        self.audit("employee.link", "employee_account_link", row["id"], new=row)
        return row

    def revoke_links(self, employee_id):
        self.profile(employee_id)
        rows = self.conn.execute(
            "UPDATE employee_account_links SET revoked_at=clock_timestamp() "
            "WHERE employee_id=%s AND revoked_at IS NULL RETURNING *",
            (employee_id,),
        ).fetchall()
        for row in rows:
            self.audit("employee.unlink", "employee_account_link", row["id"], new=row)
        return len(rows)

    def team_membership(self, employee_id, team_id, active):
        self.profile(employee_id)
        if active:
            if not self.catalog("team", team_id)["is_active"]:
                raise TenancyError("team is archived")
            row = self.conn.execute(
                "INSERT INTO employee_team_memberships (id,employee_id,team_id) "
                "VALUES (%s,%s,%s) RETURNING *",
                (uuid.uuid4(), employee_id, team_id),
            ).fetchone()
        else:
            row = self.conn.execute(
                "UPDATE employee_team_memberships SET ended_at=clock_timestamp() "
                "WHERE employee_id=%s AND team_id=%s AND ended_at IS NULL RETURNING *",
                (employee_id, team_id),
            ).fetchone()
            if row is None:
                raise TenancyError("active team membership not found")
        self.audit("team.membership", "team_membership", row["id"], new=row)
        return row

    def reporting(self, employee_id, manager_id, start, end):
        self.profile(employee_id)
        self.profile(manager_id)
        row = self.conn.execute(
            "INSERT INTO employee_reporting_relationships "
            "(id,employee_id,manager_employee_id,effective_from,effective_to) VALUES (%s,%s,%s,%s,%s) RETURNING *",
            (uuid.uuid4(), employee_id, manager_id, start, end),
        ).fetchone()
        self.audit("employee.reporting", "reporting_relationship", row["id"], new=row)
        return row

    def end_reporting(self, identity, end):
        old = self.conn.execute(
            "SELECT * FROM employee_reporting_relationships WHERE id=%s FOR UPDATE", (identity,)
        ).fetchone()
        if old is None or old["effective_to"] is not None:
            raise TenancyError("open reporting relationship not found")
        row = self.conn.execute(
            "UPDATE employee_reporting_relationships SET effective_to=%s WHERE id=%s RETURNING *",
            (end, identity),
        ).fetchone()
        self.audit("employee.reporting", "reporting_relationship", identity, old, row)
        return row

    def direct_relationship(self, employee_id, manager_id, now):
        return self.conn.execute(
            "SELECT * FROM employee_reporting_relationships WHERE employee_id=%s "
            "AND manager_employee_id=%s AND effective_from<=%s AND (effective_to IS NULL OR effective_to>%s)",
            (employee_id, manager_id, now, now),
        ).fetchone()

    def team_ids(self, employee_id):
        return [
            row["team_id"]
            for row in self.conn.execute(
                "SELECT team_id FROM employee_team_memberships WHERE employee_id=%s AND ended_at IS NULL",
                (employee_id,),
            ).fetchall()
        ]

"""Phase A workforce extensions; preserve all legacy employee and telemetry columns.

Backfill only known activation state, never job titles, SaaS links or hierarchy.
Downgrade is deliberately refused once business data exists in extension tables.
"""

from alembic import op

revision = "0005_workforce_foundation"
down_revision = "0004_manager_dashboard_auth"
branch_labels = None
depends_on = None
compatible_previous_revisions = frozenset({"0004_manager_dashboard_auth"})

CATALOGS = ("designations", "departments", "teams")
TABLES = (
    "employee_team_memberships",
    "employee_reporting_relationships",
    "employee_account_links",
    "employee_profiles",
    "teams",
    "departments",
    "designations",
)


def upgrade():
    for table in CATALOGS:
        op.execute(f"""CREATE TABLE {table} (
            id uuid PRIMARY KEY,
            display_name text NOT NULL CHECK (length(btrim(display_name)) BETWEEN 1 AND 200),
            is_active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            version bigint NOT NULL DEFAULT 1 CHECK (version > 0))""")
        op.execute(
            f"CREATE UNIQUE INDEX {table}_name_unique ON {table} (lower(btrim(display_name)))"
        )
    op.execute("""CREATE TABLE employee_profiles (
        employee_id text PRIMARY KEY REFERENCES employees(employee_id) ON DELETE RESTRICT,
        designation_id uuid REFERENCES designations(id) ON DELETE RESTRICT,
        department_id uuid REFERENCES departments(id) ON DELETE RESTRICT,
        employment_status text NOT NULL DEFAULT 'ACTIVE'
            CHECK (employment_status IN ('ACTIVE', 'LEAVE', 'INACTIVE', 'TERMINATED')),
        monitoring_enabled boolean NOT NULL DEFAULT true,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        version bigint NOT NULL DEFAULT 1 CHECK (version > 0))""")
    op.execute("""INSERT INTO employee_profiles (employee_id, employment_status, created_at)
        SELECT employee_id, CASE WHEN is_active THEN 'ACTIVE' ELSE 'INACTIVE' END, created_at FROM employees""")
    op.execute("""CREATE FUNCTION zaza_profile_on_employee() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO employee_profiles (employee_id, employment_status, created_at)
            VALUES (NEW.employee_id, CASE WHEN NEW.is_active THEN 'ACTIVE' ELSE 'INACTIVE' END, NEW.created_at);
            RETURN NEW;
        END $$""")
    op.execute(
        "CREATE TRIGGER employees_profile AFTER INSERT ON employees "
        "FOR EACH ROW EXECUTE FUNCTION zaza_profile_on_employee()"
    )
    op.execute("""CREATE TABLE employee_team_memberships (
        id uuid PRIMARY KEY,
        employee_id text NOT NULL REFERENCES employees(employee_id) ON DELETE RESTRICT,
        team_id uuid NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
        created_at timestamptz NOT NULL DEFAULT now(),
        ended_at timestamptz,
        CHECK (ended_at IS NULL OR ended_at >= created_at))""")
    op.execute(
        "CREATE UNIQUE INDEX employee_team_active_unique ON employee_team_memberships "
        "(employee_id, team_id) WHERE ended_at IS NULL"
    )
    op.execute("""CREATE TABLE employee_account_links (
        id uuid PRIMARY KEY,
        employee_id text NOT NULL REFERENCES employees(employee_id) ON DELETE RESTRICT,
        control_user_id uuid NOT NULL,
        control_membership_id uuid NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        revoked_at timestamptz,
        CHECK (revoked_at IS NULL OR revoked_at >= created_at))""")
    for column in ("employee_id", "control_membership_id", "control_user_id"):
        op.execute(
            f"CREATE UNIQUE INDEX employee_link_{column}_unique "
            f"ON employee_account_links ({column}) WHERE revoked_at IS NULL"
        )
    op.execute("""CREATE TABLE employee_reporting_relationships (
        id uuid PRIMARY KEY,
        employee_id text NOT NULL REFERENCES employees(employee_id) ON DELETE RESTRICT,
        manager_employee_id text NOT NULL REFERENCES employees(employee_id) ON DELETE RESTRICT,
        effective_from timestamptz NOT NULL,
        effective_to timestamptz,
        created_at timestamptz NOT NULL DEFAULT now(),
        CHECK (employee_id <> manager_employee_id),
        CHECK (effective_to IS NULL OR effective_to > effective_from))""")
    op.execute(
        "CREATE INDEX reporting_employee_dates ON employee_reporting_relationships "
        "(employee_id, effective_from, effective_to)"
    )
    op.execute(
        "CREATE INDEX reporting_manager_dates ON employee_reporting_relationships "
        "(manager_employee_id, effective_from, effective_to)"
    )
    # Serialize graph writes. Recursion intersects validity intervals, detecting future cycles too.
    op.execute("""CREATE FUNCTION zaza_reporting_valid() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        PERFORM pg_advisory_xact_lock(1514234449);
        IF EXISTS (SELECT 1 FROM employee_reporting_relationships r
            WHERE r.employee_id = NEW.employee_id AND r.id <> NEW.id
            AND tstzrange(r.effective_from, r.effective_to, '[)') &&
                tstzrange(NEW.effective_from, NEW.effective_to, '[)')) THEN
            RAISE EXCEPTION 'overlapping primary reporting relationship' USING ERRCODE='23514';
        END IF;
        IF EXISTS (
            WITH RECURSIVE chain(person, lo, hi, visited) AS (
                SELECT NEW.manager_employee_id, NEW.effective_from,
                       COALESCE(NEW.effective_to, 'infinity'::timestamptz), ARRAY[NEW.manager_employee_id]::text[]
                UNION ALL
                SELECT r.manager_employee_id, greatest(c.lo, r.effective_from),
                       least(c.hi, COALESCE(r.effective_to, 'infinity'::timestamptz)),
                       c.visited || r.manager_employee_id
                FROM chain c JOIN employee_reporting_relationships r ON r.employee_id = c.person
                WHERE r.id <> NEW.id AND NOT r.manager_employee_id = ANY(c.visited)
                  AND greatest(c.lo, r.effective_from) <
                      least(c.hi, COALESCE(r.effective_to, 'infinity'::timestamptz)))
            SELECT 1 FROM chain WHERE person = NEW.employee_id) THEN
            RAISE EXCEPTION 'cyclic reporting relationship' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER reporting_valid BEFORE INSERT OR UPDATE ON employee_reporting_relationships "
        "FOR EACH ROW EXECUTE FUNCTION zaza_reporting_valid()"
    )
    op.execute("""CREATE FUNCTION zaza_workforce_identity() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF to_jsonb(NEW)->TG_ARGV[0] IS DISTINCT FROM to_jsonb(OLD)->TG_ARGV[0] THEN
            RAISE EXCEPTION 'workforce identity is immutable' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END $$""")
    for table in TABLES:
        identity = "employee_id" if table == "employee_profiles" else "id"
        op.execute(
            f"CREATE TRIGGER {table}_identity BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION zaza_workforce_identity('{identity}')"
        )
    # Core employee fields and existing history stay untouched.
    op.execute("ALTER TABLE audit_logs DROP CONSTRAINT audit_logs_actor_type_valid")
    op.execute(
        "ALTER TABLE audit_logs ADD CONSTRAINT audit_logs_actor_type_valid "
        "CHECK (actor_type IN ('SYSTEM', 'CLI', 'ADMIN', 'MANAGER', 'SAAS_USER'))"
    )
    op.execute("ALTER TABLE audit_logs DROP CONSTRAINT audit_logs_entity_type_valid")
    op.execute(
        "ALTER TABLE audit_logs ADD CONSTRAINT audit_logs_entity_type_valid CHECK "
        "(entity_type IN ('employee','device','device_token','work_schedule','manager_user',"
        "'employee_profile','designation','department','team','team_membership','reporting_relationship',"
        "'employee_account_link'))"
    )


def downgrade():
    # Never silently delete extensions or their audit history. An empty expansion is reversible.
    for table in TABLES:
        if table != "employee_profiles":
            op.execute(
                f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM {table}) THEN "
                "RAISE EXCEPTION 'workforce downgrade requires reviewed data archival'; END IF; END $$"
            )
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM employee_profiles p JOIN employees e USING(employee_id)
        WHERE p.version <> 1 OR NOT p.monitoring_enabled OR p.designation_id IS NOT NULL
        OR p.department_id IS NOT NULL OR p.employment_status <>
        CASE WHEN e.is_active THEN 'ACTIVE' ELSE 'INACTIVE' END) THEN
        RAISE EXCEPTION 'workforce downgrade requires reviewed data archival'; END IF; END $$""")
    # Retain expanded audit constraints: they are backward-readable and preserve existing audit.
    op.execute("DROP TRIGGER employees_profile ON employees")
    for table in TABLES:
        op.execute(f"DROP TABLE {table}")
    op.execute("DROP FUNCTION zaza_profile_on_employee()")
    op.execute("DROP FUNCTION zaza_reporting_valid()")
    op.execute("DROP FUNCTION zaza_workforce_identity()")

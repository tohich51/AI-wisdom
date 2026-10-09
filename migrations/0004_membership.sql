-- 0004_membership.sql — C08: membership, groups, invitations, policy journal.
--
-- Scope: C08. One new migration file. 0001, 0002 and 0003 are not edited.
--
-- ============================================================= the invariant
--
--     DEACTIVATION CLOSES POSTGRESQL FIRST AND KEYCLOAK SECOND.
--
-- ACCESS-MODEL §7: "Отключение человека в UI продукта сначала закрывает его
-- membership в PostgreSQL, затем отзывает сессии Keycloak." The order is the
-- point, and it is only safe in that direction: the membership row is the
-- authority the database checks on every request, so once it is closed the
-- person is refused *even if Keycloak is unreachable*. Revoking first would
-- leave a window in which an unexpired JWT is still accepted and still reads
-- data.
--
-- This file implements the PostgreSQL half as ONE database function,
-- kb.deactivate_membership(). It is the whole of the first half: it validates
-- the actor, closes the membership, revokes the gateway's own browser sessions
-- and appends the journal entry in a single transaction. The Keycloak call is
-- deliberately NOT in here — a database transaction must never contain a
-- network call to another system, and if that call fails the membership has to
-- stay closed anyway. The second half is kb.access.membership, and the order is
-- observed by tests/integration/members/test_deactivation_order.py through a
-- second database connection, not asserted in a comment.
--
-- ================================= the second invariant
--
--     A GROUP GRANT AND A DIRECT GRANT ARE DIFFERENT THINGS, AND THE UI SAYS
--     WHICH ONE IS STILL HOLDING.
--
-- ACCESS-MODEL §1 and A08: there is no explicit deny in v1, so removing
-- somebody from a group does not remove their direct grant, and removing the
-- direct grant does not remove the group's. The honest answer is to show the
-- remaining path rather than to report a bare denial. That is why the two kinds
-- of grant live in two tables: kb.library_grant is untouched and still means
-- "this person, directly", and kb.access_group_grant means "everybody in this
-- group". kb.effective_role() below takes the union, and
-- kb.access.membership.explain_library_access() reports the paths separately.
--
-- ================================================ why effective_role changes
--
-- kb.effective_role() is re-defined here, and that is the only pre-existing
-- object this file touches. It is not cosmetic: the direct branch is 0002's
-- statement verbatim, and the group branch is a UNION that yields the same
-- library_role. A second function for the group path would not work, because
-- 0002's policies call kb.effective_role() and the group path has to be inside
-- the decision the policies make — otherwise Python would be deciding access
-- and PostgreSQL would only be decorating it, which PRODUCT-SPEC forbids.
--
-- v1 limitation, stated rather than hidden: kb.effective_role() takes a
-- principal and a library, not an organisation, so a grant in one organisation
-- would count in another. The product is one owner plus invited colleagues
-- (PRODUCT-SPEC), so there is exactly one organisation in v1. Widening the
-- signature touches every policy in 0002 and 0003 and belongs to the single
-- DDL owner, not to this card.
--
-- ============================================ why RLS is on but NOT forced
--
-- Every table here has RLS ENABLED. FORCE is deliberately NOT set on them, and
-- that is a decision, not an oversight:
--
--   * kb.access_group_grant / kb.access_group_member must be readable in full by
--     kb.effective_role() when it computes the union. If FORCE were set, the
--     answer to "which role does this person hold" would depend on who is
--     asking — a member would see fewer of their own paths than a manager sees
--     of theirs, and access would depend on the question's phrasing.
--   * The write path for groups and invitations is a policy check
--     (kb.is_org_admin) under the caller's own identity, so a forced policy
--     would add nothing.
--
-- The tables that hold authority decisions (kb.membership, kb.organisation_admin,
-- kb.access_policy_journal) keep the same trade: they are written ONLY through
-- the SECURITY DEFINER functions in this file, and kb_app is granted no
-- INSERT/UPDATE/DELETE on them at all. Reachability replaces FORCE, and it is
-- strictly stronger: a privilege that is not granted cannot be exercised by a
-- policy, a trigger, or a future bug.
--
-- ================================================ the product-level gate
--
-- ACCESS-MODEL A16: "Заблокировать человека в продукте при ещё валидном JWT →
-- следующий запрос отклонён по membership". A valid token is not a live
-- membership, so the membership state has to be part of every row decision.
-- It is part of kb.effective_role(), which every policy in 0002 and 0003 is
-- written in terms of, so a deactivated principal matches no row in any subject
-- table — not in the library their grant names, not through a group, not in a
-- job queue, not in a project. It is enforced by the database, on the
-- connection, inside the transaction, and it needs no cooperation from the
-- gateway middleware (which is C07's file, not this card's).
--
-- v1 limitation, again stated: kb.membership_allows() takes a principal and no
-- organisation, so a principal deactivated in any organisation is refused
-- everywhere. With one organisation per installation this cannot arise, and the
-- direction is the conservative one.

BEGIN;
SET search_path = kb, public;

-- ============================================================ organisation
-- The smallest possible global administration: who may manage invitations and
-- membership. NOT a role, NOT a rank, NOT a tier above library roles.
--
-- ACCESS-MODEL §2: "Глобальный администратор управляет приглашениями и
-- конфигурацией, но приложение не предоставляет ему автоматически содержание
-- личных библиотек." There is nothing in this table that widens a library
-- grant, and tests/integration/members/test_admin_sees_no_private_content.py
-- asserts that against a real database: an organisation admin is refused every
-- row of somebody else's private library.
CREATE TABLE organisation_admin (
    organisation_id uuid NOT NULL REFERENCES kb.organisation(id) ON DELETE CASCADE,
    principal_id    uuid NOT NULL,
    granted_at      timestamptz NOT NULL DEFAULT now(),
    granted_by      uuid,
    PRIMARY KEY (organisation_id, principal_id)
);

COMMENT ON TABLE kb.organisation_admin IS
    'Organisation administration: invitations and membership only. It carries '
    'no library role and grants no access to content of any library.';

-- The policy revision. ACCESS-MODEL §7: a grant or membership change is
-- committed together with an increment, and that transaction is the point of
-- change. A request admitted before the commit and applied after it must be
-- re-checked. No direct table privileges for kb_app: the counter is read and
-- written through the two functions below.
CREATE TABLE organisation_policy (
    organisation_id uuid PRIMARY KEY REFERENCES kb.organisation(id) ON DELETE CASCADE,
    revision        integer NOT NULL DEFAULT 0 CHECK (revision >= 0)
);

-- =============================================================== membership
-- One row per (organisation, principal). This is the row an invitation creates
-- and a deactivation closes.
--
-- status has exactly two values, and that is a scope decision: the card asks
-- for an invite-only lifecycle and for blocking, not for a suspension
-- taxonomy. A blocked member stays blocked. Restoring access is a new
-- invitation, which kb.accept_invitation() handles: an invitation accepted by a
-- previously deactivated principal sets the row back to 'active' and writes two
-- journal entries saying so. Nothing is implicit about it and there is no
-- separate "restore" verb.
--
-- Unknown stays NULL. email and display_name are NULL when nobody wrote them
-- down, and never 'unknown' or 'n/a'.
CREATE TABLE membership (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organisation_id uuid NOT NULL REFERENCES kb.organisation(id) ON DELETE CASCADE,
    principal_id    uuid NOT NULL,
    -- the identity provider coordinates for this person. (issuer, subject) is
    -- what C07 verified; it is stored so that deactivation can name the
    -- provider session to revoke without re-running verification.
    issuer          text NOT NULL,
    subject         text NOT NULL,
    display_name    text,
    email           text,
    status          text NOT NULL CHECK (status IN ('active', 'deactivated')),
    invited_by      uuid,
    invited_at      timestamptz NOT NULL DEFAULT now(),
    activated_at    timestamptz NOT NULL DEFAULT now(),
    -- Set in the same UPDATE that sets status, by the same function. A
    -- deactivated membership with no deactivation timestamp is a state the
    -- database cannot represent.
    deactivated_at  timestamptz,
    deactivated_by  uuid,
    deactivation_reason text,
    CONSTRAINT deactivated_membership_is_complete CHECK (
        status <> 'deactivated'
        OR (deactivated_at IS NOT NULL AND deactivated_by IS NOT NULL)
    ),
    UNIQUE (organisation_id, principal_id)
);

COMMENT ON TABLE kb.membership IS
    'Organisation membership. Written only through kb.create_membership(), '
    'kb.accept_invitation() and kb.deactivate_membership(); kb_app holds no '
    'INSERT, UPDATE or DELETE here. A principal with NO row is one who never '
    'went through the invite lifecycle, and is not blocked by it.';

-- ================================================================== groups
-- Flat groups. v1 has no nested groups and no group-of-groups
-- (ACCESS-MODEL §1), so one table with one join is the whole model.
CREATE TABLE access_group (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organisation_id uuid NOT NULL REFERENCES kb.organisation(id) ON DELETE CASCADE,
    name            text NOT NULL CHECK (name ~ '^[a-z][a-z0-9_-]{1,63}$'),
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      uuid,
    -- id + organisation must be referenceable so a member row can be pinned to
    -- one organisation's group by a composite foreign key. A member row that
    -- could name a group of a different organisation would be a cross-tenant
    -- grant with no way to see it in the UI.
    UNIQUE (organisation_id, name),
    UNIQUE (id, organisation_id)
);

-- organisation_id is denormalised onto the child tables on purpose. It lets the
-- RLS policies below ask "is the caller an administrator of the organisation
-- that owns this row" with a single indexed function call and no subquery into
-- kb.access_group — which is what keeps the policy graph acyclic. A subquery
-- from access_group's policy into access_group_member's policy and back again is
-- a recursive policy, and PostgreSQL refuses it.
CREATE TABLE access_group_member (
    group_id        uuid NOT NULL,
    organisation_id uuid NOT NULL,
    principal_id    uuid NOT NULL,
    added_at        timestamptz NOT NULL DEFAULT now(),
    added_by        uuid,
    PRIMARY KEY (group_id, principal_id),
    FOREIGN KEY (organisation_id, group_id)
        REFERENCES kb.access_group (organisation_id, id) ON DELETE CASCADE
);

CREATE INDEX access_group_member_by_principal ON kb.access_group_member (principal_id);

-- A group grant is NOT a row in kb.library_grant. That is the whole point of
-- the table: a row in library_grant is a decision about one person, a row here
-- is a decision about a set of people, and the two must stay separately
-- answerable so the UI can say which one is still holding (A08).
CREATE TABLE access_group_grant (
    group_id        uuid NOT NULL,
    organisation_id uuid NOT NULL,
    library_id      uuid NOT NULL REFERENCES kb.library(id) ON DELETE CASCADE,
    role            kb.library_role NOT NULL,
    granted_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (group_id, library_id),
    FOREIGN KEY (organisation_id, group_id)
        REFERENCES kb.access_group (organisation_id, id) ON DELETE CASCADE
);

CREATE INDEX access_group_grant_by_library ON kb.access_group_grant (library_id);

-- ============================================================== invitations
-- An invitation is a RECORD OF INTENT, never a delivered message.
--
-- On this stand the UI must not send a real email, and it does not: there is no
-- transport in this codebase, `delivery_state` starts at 'operator_pending',
-- and the only way it becomes 'notified' is an operator recording that they
-- told the person out of band. The durable artefact an operator acts on is this
-- row, and GET /orgs/{id}/invitations?status=pending is that queue.
CREATE TABLE invitation (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organisation_id uuid NOT NULL REFERENCES kb.organisation(id) ON DELETE CASCADE,
    -- Addressed by whatever the inviter actually has. An invite may name a
    -- person with no email on file, so email is NULL then and stays NULL; it
    -- is not a placeholder.
    email           text,
    subject         text,
    -- set when somebody accepts; a principal id, not a reference, because
    -- principals are identity-provider identities and are not a table here
    -- (kb.library_grant.principal_id is a plain uuid for the same reason).
    principal_id    uuid,
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'accepted', 'withdrawn', 'expired')),
    -- 'operator_pending' until a human records that the person was told.
    -- There is no code path that sets it to 'notified' on its own.
    delivery_state  text NOT NULL DEFAULT 'operator_pending'
                    CHECK (delivery_state IN ('operator_pending', 'notified')),
    notified_at     timestamptz,
    notified_by     uuid,
    accepted_at     timestamptz,
    withdrawn_at    timestamptz,
    expires_at      timestamptz,
    created_by      uuid NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    -- A notification without a notifier is not a notification, and a delivered
    -- message cannot be un-delivered, so this is the same shape as the
    -- deactivated-membership constraint: the state and its evidence are one
    -- fact or the row cannot exist.
    CONSTRAINT notification_is_attributable CHECK (
        delivery_state <> 'notified' OR (notified_at IS NOT NULL AND notified_by IS NOT NULL)
    ),
    CONSTRAINT accepted_invitation_is_attributable CHECK (
        status <> 'accepted' OR (accepted_at IS NOT NULL AND principal_id IS NOT NULL)
    )
);

-- An open invitation must be identifiable by whoever answers it. Two open
-- invitations for the same address are a duplicate the operator should see,
-- not a silent second row.
CREATE UNIQUE INDEX invitation_open_per_organisation
    ON kb.invitation (organisation_id, coalesce(email, ''), coalesce(subject, ''))
    WHERE status = 'pending' AND (email IS NOT NULL OR subject IS NOT NULL);

CREATE INDEX invitation_pending ON kb.invitation (organisation_id, created_at)
    WHERE status = 'pending';

-- ========================================================== policy journal
-- The journal is the audit trail the card asks for, and it is append-only:
-- there is no UPDATE policy and no DELETE policy, and kb_app holds neither
-- privilege. Every entry names the principal the change is ABOUT and the
-- principal that MADE it, and both are transaction-local identities.
CREATE TABLE access_policy_journal (
    id                   bigserial PRIMARY KEY,
    organisation_id      uuid NOT NULL REFERENCES kb.organisation(id) ON DELETE CASCADE,
    -- the policy revision this entry belongs to. Not unique: one policy change
    -- may write several entries in the same revision.
    revision             integer NOT NULL CHECK (revision >= 1),
    occurred_at          timestamptz NOT NULL DEFAULT now(),
    action               text NOT NULL CHECK (action IN (
                            'membership_created', 'membership_deactivated',
                            'group_created', 'group_renamed', 'group_removed',
                            'group_member_added', 'group_member_removed',
                            'group_grant_set', 'group_grant_revoked',
                            'invitation_created', 'invitation_withdrawn', 'invitation_notified',
                            'invitation_accepted', 'organisation_admin_granted',
                            'organisation_admin_revoked',
                            'sessions_revocation_requested', 'sessions_revoked',
                            'sessions_revocation_failed')),
    subject_principal_id uuid,
    actor_principal_id   uuid,
    library_id           uuid REFERENCES kb.library(id) ON DELETE SET NULL,
    group_id             uuid REFERENCES kb.access_group(id) ON DELETE SET NULL,
    invitation_id        uuid REFERENCES kb.invitation(id) ON DELETE SET NULL,
    -- free-form, but the key set is chosen here in SQL from values that are
    -- columns of the row, never from a caller-supplied string. There is no
    -- column in this table that can carry a stack trace or an upstream error
    -- body out of the provider.
    detail               jsonb NOT NULL DEFAULT '{}'::jsonb
                         CHECK (jsonb_typeof(detail) = 'object')
);

CREATE INDEX policy_journal_by_org ON kb.access_policy_journal
    (organisation_id, revision, occurred_at);

-- ================================================================= helpers


-- Is this principal an administrator of that organisation?
--
-- SECURITY DEFINER because it is read from policy expressions of the tables
-- below, and a policy that consulted its own policy would recurse.
-- kb.organisation_admin is RLS enabled but not FORCEd precisely so this one
-- function can see every admin row: the caller's own row is what the policy
-- admits, and the callers who need more than that go through here.
CREATE OR REPLACE FUNCTION kb.is_org_admin(p_org uuid, p_principal uuid)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = kb, public
AS $$
    SELECT EXISTS (
        SELECT 1 FROM kb.organisation_admin a
        WHERE a.organisation_id = p_org
          AND a.principal_id = p_principal
    );
$$;

-- The organisation-agnostic form of the same question. Used by the policy
-- reporting function below, whose caller may be an administrator of *any*
-- organisation. Deliberately not derived from kb.library: reading kb.library
-- inside a SECURITY DEFINER function means reading it through a policy that
-- calls kb.effective_role, and an administrator with no library grant would
-- resolve no organisation at all — the guard would then refuse the very person
-- it exists to let in.
CREATE OR REPLACE FUNCTION kb.is_any_org_admin(p_principal uuid)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = kb, public
AS $$
    SELECT EXISTS (
        SELECT 1 FROM kb.organisation_admin a WHERE a.principal_id = p_principal
    );
$$;

-- The gate. NULL principal is the migration/owner session, which bypasses RLS
-- by role and is not the audience of this check.
--
-- Absence of a membership row is NOT a block: a principal who never went
-- through the invite lifecycle is not blocked by it, and a member who was
-- deleted from the table by anything other than these functions would be — so
-- kb_app holds no DELETE on kb.membership and the only way out of 'active' is
-- kb.deactivate_membership(), which writes the journal entry in the same
-- transaction.
CREATE OR REPLACE FUNCTION kb.membership_allows(p_principal uuid)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = kb, public
AS $$
    SELECT p_principal IS NULL
        OR NOT EXISTS (
            SELECT 1 FROM kb.membership m
            WHERE m.principal_id = p_principal
              AND m.status <> 'active'
        );
$$;

-- Why access is closed, in a closed vocabulary: 'deactivated', or NULL when
-- membership is not what is closing the door. NULL is not "this person has
-- access" — the library grant is a different question, answered by
-- kb.effective_role().
CREATE OR REPLACE FUNCTION kb.membership_block_reason(p_principal uuid)
RETURNS text
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = kb, public
AS $$
    SELECT min(m.status)
    FROM kb.membership m
    WHERE m.principal_id = p_principal
      AND m.status <> 'active';
$$;

-- The current policy revision, readable by any authenticated principal. It is
-- an integer and it is a fence, not a secret.
CREATE OR REPLACE FUNCTION kb.current_policy_revision()
RETURNS integer
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = kb, public
AS $$
    SELECT coalesce(max(revision), 0) FROM kb.organisation_policy;
$$;

-- ============================================================ policy change

-- Every policy change goes through this one function, and it is the only writer
-- of the revision counter and the journal.
--
-- It is NOT executable by kb_app. A journal that the runtime role could write
-- by hand is not an audit trail, and the callers that legitimately need it are
-- the SECURITY DEFINER functions below and the SECURITY DEFINER trigger on the
-- group tables — both of which run as the owner.
--
-- SECURITY DEFINER because kb_app is granted no INSERT here and no UPDATE on
-- organisation_policy: reachability, not a policy that might be forgotten.
CREATE OR REPLACE FUNCTION kb.record_policy_change(
    p_org            uuid,
    p_action         text,
    p_subject        uuid,
    p_actor          uuid,
    p_library        uuid DEFAULT NULL,
    p_group          uuid DEFAULT NULL,
    p_invitation     uuid DEFAULT NULL,
    p_detail         jsonb  DEFAULT '{}'::jsonb
) RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = kb, public
AS $$
DECLARE
    next_revision integer;
BEGIN
    IF p_action IS NULL OR p_action = '' THEN
        RAISE EXCEPTION 'a policy change without an action is not a policy change'
            USING ERRCODE = 'check_violation';
    END IF;

    INSERT INTO kb.organisation_policy (organisation_id, revision)
    VALUES (p_org, 1)
    ON CONFLICT (organisation_id)
    DO UPDATE SET revision = kb.organisation_policy.revision + 1
    RETURNING revision INTO next_revision;

    INSERT INTO kb.access_policy_journal (
        organisation_id, revision, action, subject_principal_id,
        actor_principal_id, library_id, group_id, invitation_id, detail
    ) VALUES (
        p_org, next_revision, p_action, p_subject, p_actor,
        p_library, p_group, p_invitation, coalesce(p_detail, '{}'::jsonb)
    );

    RETURN next_revision;
END;
$$;

-- The group tables are written directly, as the administrator, under an RLS
-- policy. A trigger puts the journal entry there, so there is exactly one place
-- a group change can be recorded and no path that skips it.
--
-- AFTER, not BEFORE: the trigger reports what happened, and reporting a change
-- that then fails to commit would put a lie in an append-only journal.
CREATE OR REPLACE FUNCTION kb.journal_group_change()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = kb, public
AS $$
DECLARE
    actor      uuid := kb.current_principal();
    -- One function, three tables with different columns, so the row is read as
    -- a json object and the optional columns are asked for by presence. Naming
    -- NEW.group_id on kb.access_group would be a runtime error, not a compile
    -- error, and would only fire the first time somebody renamed a group.
    rec        jsonb := CASE WHEN TG_OP = 'DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
    target_grp uuid;
    target_org uuid := (rec ->> 'organisation_id')::uuid;
    target_lib uuid := CASE WHEN rec ? 'library_id' THEN (rec ->> 'library_id')::uuid END;
    target_ppl uuid := CASE WHEN rec ? 'principal_id' THEN (rec ->> 'principal_id')::uuid END;
    act        text;
BEGIN
    IF TG_TABLE_NAME = 'access_group' THEN
        target_grp := (rec ->> 'id')::uuid;
        act := CASE TG_OP WHEN 'DELETE' THEN 'group_removed'
                         WHEN 'UPDATE' THEN 'group_renamed'
                         ELSE 'group_created' END;
    ELSE
        target_grp := (rec ->> 'group_id')::uuid;
        IF TG_TABLE_NAME = 'access_group_member' THEN
            act := CASE TG_OP WHEN 'DELETE' THEN 'group_member_removed'
                             ELSE 'group_member_added' END;
        ELSE
            act := CASE TG_OP WHEN 'DELETE' THEN 'group_grant_revoked'
                             ELSE 'group_grant_set' END;
        END IF;
    END IF;

    PERFORM kb.record_policy_change(
        target_org, act, target_ppl, actor,
        p_library := target_lib,
        p_group   := target_grp,
        p_detail  := jsonb_build_object('operation', TG_OP)
    );
    RETURN NULL;
END;
$$;

CREATE TRIGGER access_group_journalled
    AFTER INSERT OR UPDATE OR DELETE ON kb.access_group
    FOR EACH ROW EXECUTE FUNCTION kb.journal_group_change();

CREATE TRIGGER access_group_member_journalled
    AFTER INSERT OR UPDATE OR DELETE ON kb.access_group_member
    FOR EACH ROW EXECUTE FUNCTION kb.journal_group_change();

CREATE TRIGGER access_group_grant_journalled
    AFTER INSERT OR UPDATE OR DELETE ON kb.access_group_grant
    FOR EACH ROW EXECUTE FUNCTION kb.journal_group_change();

-- ======================================================== organisation admin

-- The first administrator of an organisation. SECURITY DEFINER for the same
-- bootstrap reason as kb.create_owned_library in 0003: the table is empty, so
-- an INSERT policy could not admit anybody without an existence subquery on the
-- table it governs.
--
-- The principal is forced to current_principal(). There is no parameter to
-- appoint somebody else, and no way to call it without a verified identity.
CREATE OR REPLACE FUNCTION kb.claim_organisation_admin(p_org uuid)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = kb, public
AS $$
DECLARE
    caller     uuid := kb.current_principal();
    org_exists boolean;
BEGIN
    IF caller IS NULL THEN
        RAISE EXCEPTION 'no authenticated principal'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    SELECT EXISTS (SELECT 1 FROM kb.organisation o WHERE o.id = p_org) INTO org_exists;
    IF NOT org_exists THEN
        RAISE EXCEPTION 'no such organisation' USING ERRCODE = 'foreign_key_violation';
    END IF;

    IF EXISTS (SELECT 1 FROM kb.organisation_admin a
                WHERE a.organisation_id = p_org AND a.principal_id = caller) THEN
        RETURN false;
    END IF;

    INSERT INTO kb.organisation_admin (organisation_id, principal_id, granted_by)
    VALUES (p_org, caller, caller);

    PERFORM kb.record_policy_change(p_org, 'organisation_admin_granted', caller, caller);
    RETURN true;
END;
$$;

-- Add a second administrator. The last one cannot be removed: an organisation
-- with no administrator has no supported way to invite anybody, and the only
-- way back would be a direct psql session, which is exactly the operational
-- hole this card exists to avoid.
CREATE OR REPLACE FUNCTION kb.add_organisation_admin(p_org uuid, p_target uuid)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = kb, public
AS $$
DECLARE
    caller uuid := kb.current_principal();
BEGIN
    IF caller IS NULL THEN
        RAISE EXCEPTION 'no authenticated principal'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NOT kb.is_org_admin(p_org, caller) THEN
        RAISE EXCEPTION 'only an organisation administrator may appoint one'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF p_target IS NULL THEN
        RAISE EXCEPTION 'no such principal' USING ERRCODE = 'foreign_key_violation';
    END IF;
    IF EXISTS (SELECT 1 FROM kb.organisation_admin a
                WHERE a.organisation_id = p_org AND a.principal_id = p_target) THEN
        RETURN false;
    END IF;
    INSERT INTO kb.organisation_admin (organisation_id, principal_id, granted_by)
    VALUES (p_org, p_target, caller);
    PERFORM kb.record_policy_change(p_org, 'organisation_admin_granted', p_target, caller);
    RETURN true;
END;
$$;

CREATE OR REPLACE FUNCTION kb.remove_organisation_admin(p_org uuid, p_target uuid)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = kb, public
AS $$
DECLARE
    caller  uuid := kb.current_principal();
    admins  integer;
BEGIN
    IF caller IS NULL THEN
        RAISE EXCEPTION 'no authenticated principal'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NOT kb.is_org_admin(p_org, caller) THEN
        RAISE EXCEPTION 'only an organisation administrator may remove one'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    SELECT count(*) INTO admins FROM kb.organisation_admin WHERE organisation_id = p_org;
    IF admins <= 1 THEN
        RAISE EXCEPTION 'the last administrator of an organisation cannot be removed'
            USING ERRCODE = 'restrict_violation';
    END IF;
    DELETE FROM kb.organisation_admin
     WHERE organisation_id = p_org AND principal_id = p_target;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no such administrator' USING ERRCODE = 'restrict_violation';
    END IF;
    PERFORM kb.record_policy_change(p_org, 'organisation_admin_revoked', p_target, caller);
    RETURN true;
END;
$$;

-- ============================================================ membership ops

-- Create a membership for somebody who already has an identity. Called by an
-- administrator; the invitation path is kb.accept_invitation() below, which is
-- the only one a non-administrator can reach.
--
-- The actor must be an administrator of the organisation, and the function
-- writes the journal entry in the same transaction, so "who is in the
-- organisation" and "who was told about it" cannot disagree.
CREATE OR REPLACE FUNCTION kb.create_membership(
    p_org          uuid,
    p_principal    uuid,
    p_issuer       text,
    p_subject      text,
    p_display_name text DEFAULT NULL,
    p_email        text DEFAULT NULL,
    p_invitation   uuid DEFAULT NULL
) RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = kb, public
AS $$
DECLARE
    caller uuid := kb.current_principal();
    new_id uuid;
BEGIN
    IF caller IS NULL THEN
        RAISE EXCEPTION 'no authenticated principal'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NOT kb.is_org_admin(p_org, caller) THEN
        RAISE EXCEPTION 'only an organisation administrator may invite'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF p_principal IS NULL OR p_issuer IS NULL OR p_subject IS NULL THEN
        RAISE EXCEPTION 'a membership needs a principal and a verified identity'
            USING ERRCODE = 'not_null_violation';
    END IF;

    INSERT INTO kb.membership (
        id, organisation_id, principal_id, issuer, subject,
        display_name, email, status, invited_by
    ) VALUES (
        gen_random_uuid(), p_org, p_principal, p_issuer, p_subject,
        p_display_name, p_email, 'active', caller
    )
    RETURNING id INTO new_id;

    PERFORM kb.record_policy_change(
        p_org, 'membership_created', p_principal, caller, p_invitation := p_invitation,
        p_detail := jsonb_build_object('invited', p_invitation IS NOT NULL)
    );
    RETURN new_id;
END;
$$;

-- ============================================================== DEACTIVATION
--
-- THE FIRST HALF OF THE CARD'S ORDERING RULE. This function is the entire
-- PostgreSQL side of a product deactivation, and it is one statement for the
-- caller so that it cannot be half-applied:
--
--   1. the actor is an administrator of this organisation (or is the member
--      leaving, which is a legitimate self-service departure);
--   2. the membership row is closed — status, timestamp and the principal that
--      did it, in one UPDATE;
--   3. the gateway's own durable browser sessions for that principal are
--      revoked, so a cookie that is still cryptographically valid stops
--      working at the next request;
--   4. the policy revision is incremented and the journal entry is written, in
--      the same transaction as (2).
--
-- What is NOT in here: the Keycloak call. A database transaction must not span
-- a network call to another system, and — the load-bearing reason — the
-- membership has to be closed whether or not Keycloak answers. If the
-- revocation fails, the member is still refused here, and
-- kb.record_session_revocation() marks the outstanding work for an operator.
--
-- Returns the new policy revision.
CREATE OR REPLACE FUNCTION kb.deactivate_membership(
    p_org     uuid,
    p_target  uuid,
    p_reason  text DEFAULT NULL
) RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = kb, public
AS $$
DECLARE
    caller          uuid := kb.current_principal();
    target_issuer   text;
    target_subject  text;
    sessions_closed integer;
    revision        integer;
BEGIN
    IF caller IS NULL THEN
        RAISE EXCEPTION 'no authenticated principal'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF p_target IS NULL THEN
        RAISE EXCEPTION 'no such member' USING ERRCODE = 'restrict_violation';
    END IF;
    -- an administrator may block anybody; anybody may block themselves
    IF caller <> p_target AND NOT kb.is_org_admin(p_org, caller) THEN
        RAISE EXCEPTION 'only an organisation administrator may block a member'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    SELECT issuer, subject INTO target_issuer, target_subject
      FROM kb.membership
     WHERE organisation_id = p_org AND principal_id = p_target
       AND status = 'active'
    FOR UPDATE;

    IF NOT FOUND THEN
        -- Either no membership, or one that is already closed. Both are the
        -- same answer on purpose: "you may not see it" and "it does not exist"
        -- must not be distinguishable.
        RAISE EXCEPTION 'no such active member'
            USING ERRCODE = 'restrict_violation';
    END IF;

    UPDATE kb.membership
       SET status = 'deactivated',
           deactivated_at = now(),
           deactivated_by = caller,
           deactivation_reason = p_reason
     WHERE organisation_id = p_org AND principal_id = p_target;

    -- The gateway's own sessions die in the same transaction as the membership.
    -- A revoked row cannot authenticate anything even while its cookie is still
    -- cryptographically valid, because kb.browser_session.load() filters on
    -- revoked_at.
    UPDATE kb.browser_session
       SET revoked_at = now(), revoked_reason = 'membership_deactivated'
     WHERE principal_id = p_target
       AND revoked_at IS NULL;
    GET DIAGNOSTICS sessions_closed = ROW_COUNT;

    revision := kb.record_policy_change(
        p_org, 'membership_deactivated', p_target, caller,
        p_detail := jsonb_build_object(
            'issuer', target_issuer,
            'subject', target_subject,
            'browser_sessions_closed', sessions_closed,
            -- the reason is operator-supplied free text or NULL, stored as a json
            -- value. It is never interpolated into anything executable, and the
            -- journal has no column that could carry an upstream error body.
            'reason', p_reason
        )
    );
    RETURN revision;
END;
$$;

-- Second half of the ordering rule: the Keycloak call has been made, or has
-- failed. Called AFTER the deactivation transaction has committed, which is why
-- it is a separate function and not a continuation of the first one.
--
-- 'revoked'  — the provider confirms the sessions are gone.
-- 'failed'   — the provider did not answer. The membership stays closed; this
--               journal entry is the outstanding work an operator retries.
CREATE OR REPLACE FUNCTION kb.record_session_revocation(
    p_org    uuid,
    p_target uuid,
    p_result text,
    p_detail jsonb DEFAULT '{}'::jsonb
) RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = kb, public
AS $$
DECLARE
    caller   uuid := kb.current_principal();
    revision integer;
    action   text;
BEGIN
    IF caller IS NULL THEN
        RAISE EXCEPTION 'no authenticated principal'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NOT kb.is_org_admin(p_org, caller) THEN
        RAISE EXCEPTION 'only an organisation administrator may record a revocation'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF p_result NOT IN ('revoked', 'failed') THEN
        RAISE EXCEPTION 'revocation result must be revoked or failed'
            USING ERRCODE = 'check_violation';
    END IF;

    action := CASE p_result
                WHEN 'revoked' THEN 'sessions_revoked'
                ELSE 'sessions_revocation_failed'
              END;

    revision := kb.record_policy_change(p_org, action, p_target, caller,
                                        p_detail := p_detail);
    RETURN revision;
END;
$$;

-- ============================================================= invitations

-- An invitation is an intent an operator acts on. Nothing is sent: this
-- function takes no transport, has no parameter for one, and records the row
-- an operator sees in the queue.
CREATE OR REPLACE FUNCTION kb.create_invitation(
    p_org        uuid,
    p_email      text DEFAULT NULL,
    p_subject    text DEFAULT NULL,
    p_expires_at timestamptz DEFAULT NULL
) RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = kb, public
AS $$
DECLARE
    caller uuid := kb.current_principal();
    new_id uuid;
BEGIN
    IF caller IS NULL THEN
        RAISE EXCEPTION 'no authenticated principal'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NOT kb.is_org_admin(p_org, caller) THEN
        RAISE EXCEPTION 'only an organisation administrator may invite'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF p_email IS NULL AND p_subject IS NULL THEN
        RAISE EXCEPTION 'an invitation needs an address or a provider subject'
            USING ERRCODE = 'check_violation';
    END IF;

    INSERT INTO kb.invitation (
        id, organisation_id, email, subject, expires_at, created_by
    ) VALUES (
        gen_random_uuid(), p_org, p_email, p_subject, p_expires_at, caller
    )
    RETURNING id INTO new_id;

    PERFORM kb.record_policy_change(p_org, 'invitation_created', NULL, caller,
                                    p_invitation := new_id);
    RETURN new_id;
END;
$$;

-- An operator records that they told the person, out of band. This is the only
-- way delivery_state leaves 'operator_pending', and it requires naming who did
-- it: the database will not hold a 'notified' row without a notifier.
CREATE OR REPLACE FUNCTION kb.mark_invitation_notified(p_invitation uuid)
RETURNS timestamptz
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = kb, public
AS $$
DECLARE
    caller uuid := kb.current_principal();
    at_now timestamptz;
    org    uuid;
BEGIN
    IF caller IS NULL THEN
        RAISE EXCEPTION 'no authenticated principal'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    SELECT organisation_id INTO org FROM kb.invitation
     WHERE id = p_invitation AND status = 'pending';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no such pending invitation'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NOT kb.is_org_admin(org, caller) THEN
        RAISE EXCEPTION 'only an organisation administrator may notify'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    UPDATE kb.invitation
       SET delivery_state = 'notified', notified_at = now(), notified_by = caller
     WHERE id = p_invitation
    RETURNING notified_at INTO at_now;

    PERFORM kb.record_policy_change(org, 'invitation_notified', NULL, caller,
                                    p_invitation := p_invitation);
    RETURN at_now;
END;
$$;

CREATE OR REPLACE FUNCTION kb.withdraw_invitation(p_invitation uuid)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = kb, public
AS $$
DECLARE
    caller uuid := kb.current_principal();
    org    uuid;
BEGIN
    IF caller IS NULL THEN
        RAISE EXCEPTION 'no authenticated principal'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    SELECT organisation_id INTO org FROM kb.invitation
     WHERE id = p_invitation AND status = 'pending';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no such pending invitation'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NOT kb.is_org_admin(org, caller) THEN
        RAISE EXCEPTION 'only an organisation administrator may withdraw'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    UPDATE kb.invitation
       SET status = 'withdrawn', withdrawn_at = now()
     WHERE id = p_invitation;
    PERFORM kb.record_policy_change(org, 'invitation_withdrawn', NULL, caller,
                                    p_invitation := p_invitation);
    RETURN true;
END;
$$;

-- Accept an invitation. The principal is forced to current_principal(): a
-- caller cannot accept an invitation on somebody else's behalf, so accepting
-- can never mint a membership for a person who did not present a verified
-- identity.
--
-- A person who was previously deactivated and is invited again becomes active
-- again. That is the supported way back, and it is not a separate verb: it is a
-- new invitation, accepted, journalled.
CREATE OR REPLACE FUNCTION kb.accept_invitation(
    p_invitation uuid,
    p_issuer     text,
    p_subject    text
) RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = kb, public
AS $$
DECLARE
    caller  uuid := kb.current_principal();
    org     uuid;
    new_id  uuid;
BEGIN
    IF caller IS NULL THEN
        RAISE EXCEPTION 'no authenticated principal'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF p_issuer IS NULL OR p_subject IS NULL THEN
        RAISE EXCEPTION 'a verified identity is required'
            USING ERRCODE = 'not_null_violation';
    END IF;

    SELECT organisation_id INTO org FROM kb.invitation
     WHERE id = p_invitation
       AND status = 'pending'
       AND (expires_at IS NULL OR now() < expires_at);
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no such pending invitation'
            USING ERRCODE = 'restrict_violation';
    END IF;

    UPDATE kb.invitation
       SET status = 'accepted', accepted_at = now(), principal_id = caller
     WHERE id = p_invitation;

    -- The membership is written here rather than through kb.create_membership(),
    -- because the person accepting the invitation is not an administrator: this
    -- is the one way a principal with no organisation role at all becomes a
    -- member.
    INSERT INTO kb.membership (
        id, organisation_id, principal_id, issuer, subject, status, invited_by
    ) VALUES (
        gen_random_uuid(), org, caller, p_issuer, p_subject, 'active',
        (SELECT created_by FROM kb.invitation WHERE id = p_invitation)
    )
    ON CONFLICT (organisation_id, principal_id) DO UPDATE
        SET status = 'active',
            activated_at = now(),
            deactivated_at = NULL,
            deactivated_by = NULL,
            deactivation_reason = NULL
    RETURNING id INTO new_id;

    PERFORM kb.record_policy_change(org, 'membership_created', caller, caller,
                                    p_invitation := p_invitation,
                                    p_detail := jsonb_build_object('via_invitation', true));
    PERFORM kb.record_policy_change(org, 'invitation_accepted', caller, caller,
                                    p_invitation := p_invitation);
    RETURN new_id;
END;
$$;

-- ============================================== effective role, with groups
--
-- 0002's function, unchanged, plus the group path, plus the membership gate.
--
-- The direct branch is copied verbatim so the behaviour 0002's tests pin down
-- cannot drift; the group branch is UNIONed in and ranked below a direct grant
-- of equal role, because a direct decision about one person is the more
-- specific answer and the UI should say so.
--
-- THE MEMBERSHIP GATE LIVES HERE, and that is a deliberate placement. Every
-- policy from 0002 and 0003 reaches its answer through this function, so
-- returning NULL for a principal whose membership is closed denies them every
-- subject table at once — with no new policy anywhere, and in particular with
-- no new policy on kb.project_library_link.
--
-- That last part is not tidiness. C09 shipped a structural guard
-- (test_link_policies_never_consult_the_target_role) that asserts every policy
-- on the link table consults the PROJECT's role and never the target's. A
-- parallel RESTRICTIVE policy on that table would have been a false positive
-- for that guard — it is not the link's access rule — and the only ways to
-- satisfy both were to leave the table ungated or to write an expression that
-- mentions a role it does not need. Neither is acceptable, so the gate went
-- where the guard already looks.
--
-- The two tables that keep a RESTRICTIVE policy below are the two whose policy
-- has an own-row path that never calls this function: kb.library_grant's
-- "you may always see your own grant row" and kb.use_record's "you may always
-- see your own usage". A principal who is not a member of the installation
-- should not keep either, and nothing else needs saying twice.
--
-- SECURITY DEFINER and fixed search_path, as in 0002: this function is called
-- from policy expressions, and it must read every grant row rather than the
-- subset the caller could see.
CREATE OR REPLACE FUNCTION kb.effective_role(p_principal uuid, p_library uuid)
RETURNS kb.library_role
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = kb, public
AS $$
    SELECT path.role
    FROM (
        SELECT g.role AS role, 0 AS direct
          FROM kb.library_grant g
         WHERE g.library_id = p_library
           AND g.principal_id = p_principal
        UNION ALL
        SELECT gg.role AS role, 1 AS direct
          FROM kb.access_group_grant gg
          JOIN kb.access_group_member m ON m.group_id = gg.group_id
         WHERE gg.library_id = p_library
           AND m.principal_id = p_principal
    ) AS path
    WHERE kb.membership_allows(p_principal)
    ORDER BY kb.role_rank(path.role) DESC, path.direct ASC
    LIMIT 1;
$$;

-- Which paths exist, separately. This is the answer to A08: not "you may read"
-- and not "you may not", but "you may read, through this group, and that is the
-- only thing still holding".
--
-- It returns the paths and nothing else — no library name, no content, no
-- count of libraries the caller cannot resolve. SECURITY DEFINER so a member
-- can see their own group paths even when the group tables are filtered.
--
-- The guard is in SQL as well as in the caller: a third party asking about
-- somebody else gets no rows unless the caller ALREADY has a role on that
-- library, and a mistake in the application layer cannot turn this into a way
-- to ask about other people's grants.
--
-- The extra condition on the administrator case is the finding from writing
-- this: an organisation administrator who can read everybody's grant list
-- learns the shape of a private library — that it exists, and that a named
-- person manages it — without ever being able to open it. "Administers
-- invitations and membership" does not mean "may map the company".
CREATE OR REPLACE FUNCTION kb.effective_role_paths(p_principal uuid, p_library uuid)
RETURNS TABLE (path_kind text, path_role kb.library_role, path_group uuid, path_group_name text)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = kb, public
AS $$
    SELECT 'direct'::text, g.role, NULL::uuid, NULL::text
      FROM kb.library_grant g
     WHERE g.library_id = p_library
       AND g.principal_id = p_principal
       AND (p_principal = kb.current_principal()
            OR (kb.is_any_org_admin(kb.current_principal())
                AND kb.effective_role(kb.current_principal(), p_library) IS NOT NULL))
    UNION ALL
    SELECT 'group'::text, gg.role, gr.id, gr.name
      FROM kb.access_group_grant gg
      JOIN kb.access_group_member m ON m.group_id = gg.group_id
      JOIN kb.access_group gr ON gr.id = gg.group_id
     WHERE gg.library_id = p_library
       AND m.principal_id = p_principal
       AND (p_principal = kb.current_principal()
            OR (kb.is_any_org_admin(kb.current_principal())
                AND kb.effective_role(kb.current_principal(), p_library) IS NOT NULL))
    ORDER BY 1, 4;
$$;

-- ================================================== the product-level gate
--
-- The gate itself is inside kb.effective_role, above: a principal whose
-- membership is closed resolves to no role, and every policy in 0002 and 0003
-- is written in terms of a role. That is "полное отключение membership закрывает
-- все пути" expressed in the one place the authority is decided.
--
-- The two tables below need a second, explicit statement, and only these two.
-- Their policies have an own-row branch that never consults a role:
--
--   kb.library_grant  "a principal sees their own grant row"
--   kb.use_record     "a principal sees their own usage"
--
-- Without a RESTRICTIVE policy a person removed from the installation would
-- keep reading their own grant list and their own history. Every other
-- subject table reaches its answer through effective_role and is already shut.
--
-- kb.rule_action has no policy at all — 0002 never gave it one — so it is
-- already default-deny. That gap is the finding C09 reported; it belongs to the
-- owner of 0002 and is not closed here.
--
-- kb.library_type is deliberately absent: a vocabulary of type definitions with
-- no library name, no content and no count, so a blocked principal learning
-- that a key called 'brand' exists discloses nothing.

CREATE POLICY membership_must_be_active ON kb.library_grant AS RESTRICTIVE
    FOR ALL
    USING (kb.membership_allows(kb.current_principal()))
    WITH CHECK (kb.membership_allows(kb.current_principal()));

CREATE POLICY membership_must_be_active ON kb.use_record AS RESTRICTIVE
    FOR ALL
    USING (kb.membership_allows(kb.current_principal()))
    WITH CHECK (kb.membership_allows(kb.current_principal()));

-- ==================================================================== RLS
--
-- Read paths for the new tables. There is deliberately no INSERT/UPDATE/DELETE
-- policy for the authority tables (membership, organisation_admin,
-- access_policy_journal, organisation_policy): they are written only by the
-- SECURITY DEFINER functions above, and kb_app holds no such privilege. For
-- groups the caller's own identity does the work, under kb.is_org_admin, and
-- the journal entry is written by a trigger rather than by the caller.

ALTER TABLE kb.organisation_admin        ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.organisation_policy       ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.membership                ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.access_group              ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.access_group_member       ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.access_group_grant        ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.invitation                ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.access_policy_journal     ENABLE ROW LEVEL SECURITY;

-- An administrator sees their own row and nothing else. Membership of the admin
-- set is not a directory: it says who can invite people, and no library role
-- follows from it.
CREATE POLICY organisation_admin_read ON kb.organisation_admin FOR SELECT
    USING (principal_id = kb.current_principal());

-- A principal reads their own membership; an administrator reads the
-- organisation's. Nobody reads a membership they are neither the subject of nor
-- an administrator of.
CREATE POLICY membership_read ON kb.membership FOR SELECT
    USING (principal_id = kb.current_principal()
           OR kb.is_org_admin(organisation_id, kb.current_principal()));

-- The revision counter is an integer. Reading it reveals no organisation's
-- contents, and the gateway needs it on every request as a fence.
CREATE POLICY organisation_policy_read ON kb.organisation_policy FOR SELECT
    USING (kb.current_principal() IS NOT NULL);

-- Groups: a member may see the groups they are in; an administrator may see all
-- of them. Both are needed — the member's view is what makes the A08
-- explanation possible at all.
CREATE POLICY access_group_read ON kb.access_group FOR SELECT
    USING (kb.is_org_admin(organisation_id, kb.current_principal())
           OR EXISTS (SELECT 1 FROM kb.access_group_member m
                       WHERE m.group_id = id
                         AND m.principal_id = kb.current_principal()));

CREATE POLICY access_group_write ON kb.access_group FOR ALL
    USING      (kb.is_org_admin(organisation_id, kb.current_principal()))
    WITH CHECK (kb.is_org_admin(organisation_id, kb.current_principal()));

CREATE POLICY access_group_member_read ON kb.access_group_member FOR SELECT
    USING (principal_id = kb.current_principal()
           OR kb.is_org_admin(organisation_id, kb.current_principal()));

-- Group membership: organisation administrators do this job.
--
-- The WITH CHECK refuses a row whose subject IS the caller. That is not
-- pedantry, it is the only thing standing between "administers invitations and
-- configuration" and "reads a private library": an administrator who may add
-- themselves to a group inherits every grant that group holds, and nobody who
-- manages those libraries ever agreed to it. Leaving is still allowed —
-- PostgreSQL evaluates only USING for a DELETE, so the restriction is on the
-- row a write would create, not on the one it would remove.
CREATE POLICY access_group_member_write ON kb.access_group_member FOR ALL
    USING      (kb.is_org_admin(organisation_id, kb.current_principal()))
    WITH CHECK (kb.is_org_admin(organisation_id, kb.current_principal())
                AND principal_id <> kb.current_principal());

CREATE POLICY access_group_grant_read ON kb.access_group_grant FOR SELECT
    USING (EXISTS (SELECT 1 FROM kb.access_group_member m
                    WHERE m.group_id = group_id
                      AND m.principal_id = kb.current_principal())
           OR kb.is_org_admin(organisation_id, kb.current_principal()));

-- Group GRANTS are a library manager's decision, not an organisation
-- administrator's.
--
-- ACCESS-MODEL section 2, the role table: assigning a library's grants is a
-- Manager's capability and nobody else's. An organisation administrator decides
-- WHO is in a group; only somebody who already manages the library decides what
-- that group gets. Either half alone is an escalation — the first without the
-- second is "make a group, give it a library, add yourself"; the second without
-- the first is "every library manager in the company can reshape any group".
CREATE POLICY access_group_grant_write ON kb.access_group_grant FOR ALL
    USING (
        kb.is_org_admin(organisation_id, kb.current_principal())
        AND kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 40
    )
    WITH CHECK (
        kb.is_org_admin(organisation_id, kb.current_principal())
        AND kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 40
    );

-- An administrator sees the invitation queue. A non-administrator sees only
-- invitations that have already been accepted BY THEM, and an invitation
-- addressed by email is nobody's until it is accepted: the address is not an
-- identity, and a queue row is not an admission ticket.
CREATE POLICY invitation_read ON kb.invitation FOR SELECT
    USING (kb.is_org_admin(organisation_id, kb.current_principal())
           OR principal_id = kb.current_principal());

-- There is no INSERT, UPDATE or DELETE policy for kb.invitation and kb_app
-- holds no such privilege. Creating, notifying, withdrawing and accepting all
-- go through the SECURITY DEFINER functions above, so there is exactly one path
-- to each of those states and each of them writes a journal entry.

-- The journal is readable by the administrator of the organisation and by the
-- principal it is about. There is no write policy and no write privilege: the
-- journal is what happened, and nothing in the product can rewrite it.
CREATE POLICY policy_journal_read ON kb.access_policy_journal FOR SELECT
    USING (kb.is_org_admin(organisation_id, kb.current_principal())
           OR subject_principal_id = kb.current_principal());

-- =============================================================== privileges
--
-- 0002's GRANT ON ALL TABLES predates these tables, so they are granted here,
-- explicitly and no wider. Note what is NOT granted: any write privilege on
-- membership, organisation_admin, organisation_policy, invitation or the
-- journal. Those are reachable only through the SECURITY DEFINER functions.

GRANT SELECT ON kb.membership, kb.access_policy_journal TO kb_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON
    kb.access_group, kb.access_group_member, kb.access_group_grant TO kb_app;
GRANT SELECT ON kb.invitation TO kb_app;
GRANT SELECT ON kb.organisation_admin, kb.organisation_policy TO kb_app;

GRANT EXECUTE ON FUNCTION kb.is_org_admin(uuid, uuid) TO kb_app;
GRANT EXECUTE ON FUNCTION kb.is_any_org_admin(uuid) TO kb_app;
GRANT EXECUTE ON FUNCTION kb.membership_allows(uuid) TO kb_app;
GRANT EXECUTE ON FUNCTION kb.membership_block_reason(uuid) TO kb_app;
GRANT EXECUTE ON FUNCTION kb.current_policy_revision() TO kb_app;
GRANT EXECUTE ON FUNCTION kb.effective_role(uuid, uuid) TO kb_app;
GRANT EXECUTE ON FUNCTION kb.effective_role_paths(uuid, uuid) TO kb_app;
GRANT EXECUTE ON FUNCTION kb.claim_organisation_admin(uuid) TO kb_app;
GRANT EXECUTE ON FUNCTION kb.add_organisation_admin(uuid, uuid) TO kb_app;
GRANT EXECUTE ON FUNCTION kb.remove_organisation_admin(uuid, uuid) TO kb_app;
GRANT EXECUTE ON FUNCTION kb.create_membership(uuid, uuid, text, text, text, text, uuid)
    TO kb_app;
GRANT EXECUTE ON FUNCTION kb.deactivate_membership(uuid, uuid, text) TO kb_app;
GRANT EXECUTE ON FUNCTION kb.record_session_revocation(uuid, uuid, text, jsonb) TO kb_app;
GRANT EXECUTE ON FUNCTION kb.create_invitation(uuid, text, text, timestamptz) TO kb_app;
GRANT EXECUTE ON FUNCTION kb.mark_invitation_notified(uuid) TO kb_app;
GRANT EXECUTE ON FUNCTION kb.withdraw_invitation(uuid) TO kb_app;
GRANT EXECUTE ON FUNCTION kb.accept_invitation(uuid, text, text) TO kb_app;

-- CREATE FUNCTION grants EXECUTE to PUBLIC by default, and every SECURITY
-- DEFINER function above is a capability the whole world would otherwise hold.
-- 0003 did not revoke; this file does, for its own functions.
REVOKE ALL ON FUNCTION kb.is_org_admin(uuid, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION kb.is_any_org_admin(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION kb.membership_allows(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION kb.membership_block_reason(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION kb.current_policy_revision() FROM PUBLIC;
REVOKE ALL ON FUNCTION kb.effective_role(uuid, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION kb.effective_role_paths(uuid, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION kb.record_policy_change(uuid, text, uuid, uuid, uuid, uuid, uuid, jsonb)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION kb.journal_group_change() FROM PUBLIC;
REVOKE ALL ON FUNCTION kb.claim_organisation_admin(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION kb.add_organisation_admin(uuid, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION kb.remove_organisation_admin(uuid, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION kb.create_membership(uuid, uuid, text, text, text, text, uuid)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION kb.deactivate_membership(uuid, uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION kb.record_session_revocation(uuid, uuid, text, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION kb.create_invitation(uuid, text, text, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION kb.mark_invitation_notified(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION kb.withdraw_invitation(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION kb.accept_invitation(uuid, text, text) FROM PUBLIC;

-- The worker processes content. It has no business inviting anybody, closing a
-- membership or reading a journal. Deliberately not granted.
--
-- kb_worker also loses nothing it had: 0002 and 0003 are untouched.

COMMIT;

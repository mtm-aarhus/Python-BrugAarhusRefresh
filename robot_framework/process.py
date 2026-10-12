"""This module contains the main process of the robot.

The robot syncs udeservering tilladelser from Deskpro into
BrugAarhus_Udeservering — nothing else. The fakturalinjer are BrugAarhus
Kassen's job: after the sync the robot asks kassen to bring the lines of the
synced tilladelser up to date (POST /udeservering/api/fakturalinjer/dan), which
keeps the rolling horizon moving and picks up changes from Deskpro.
"""

from OpenOrchestrator.orchestrator_connection.connection import OrchestratorConnection
from OpenOrchestrator.database.queues import QueueElement
import requests
import pyodbc
from datetime import datetime, date
from zoneinfo import ZoneInfo


# =============================================================================
# DESKPRO FIELD IDS
# Always use IDs — Deskpro titles get renamed during prototyping.
# =============================================================================
FIELD_FIRMANAVN = "55"             # Restaurationens / virksomhedens navn
FIELD_ADRESSE = "255"              # Restaurationens / virksomhedens adresse
FIELD_CVR = "1258"                 # CVR (number)
FIELD_WORKFLOW = "1147"            # Workflow (sagsstatus)

# Sagsdata
FIELD_ZONE = "1216"                # Zone (1 / 2)
FIELD_UDESERVERINGSTYPE = "1192"   # Alm. udeservering / Parklet / Specialtilladelse
FIELD_SERVERINGSAREAL = "1196"     # m2 — all types
FIELD_FACADELAENGDE = "1210"       # the form shows it for Alm. udeservering

# Periode — shared by all types, both set by the sagsbehandler. A slutdato
# works like an opsigelse; no slutdato = fortløbende.
FIELD_STARTDATO = "1291"
FIELD_SLUTDATO = "1292"

# Sæson — kassen bills from the mirrors stored below (Sommersaeson "Ja",
# Vintermaaneder "Oktober, November"). Which types show which season field is
# decided by the Deskpro form.
FIELD_SOMMERSAESON = "64"          # Radio: Ja (65) / Nej (66)
FIELD_VINTERSAESON = "67"          # Checkbox multi: the winter months

# Deskpro department of the udeservering form ("TEST - Udeservering").
DEPARTMENT_ID = 23

# How many tilladelser per call to kassen's fakturalinje API (max 500).
KASSEN_CHUNK = 100


# =============================================================================
# OPTION IDS
# =============================================================================
# Udeserveringstype (field 1192)
OPT_TYPE_ALM = 1193        # "Alm. udeservering (fortløbende)"
OPT_TYPE_SPECIAL = 1194    # "Specialtilladelse (tidsbegrænset)" — never invoiced
OPT_TYPE_PARKLET = 1195    # "Parklet (pr. sæson)"
TYPE_FALLBACK_TITLE = {
    OPT_TYPE_ALM: "Alm. udeservering (fortløbende)",
    OPT_TYPE_SPECIAL: "Specialtilladelse (tidsbegrænset)",
    OPT_TYPE_PARKLET: "Parklet (pr. sæson)",
}

OPT_WORKFLOW_GODKENDT = 1151   # "Ansøgning godkendt" → the tilladelse enters kassen

OPT_SOMMER_JA = 65
OPT_SOMMER_NEJ = 66

# Vintersæson option → month name. The option-id order (68→73) is the natural
# Danish winter-season order (Oktober → Marts).
WINTER_OPT_TO_NAME = {
    68: "Oktober",
    69: "November",
    70: "December",
    71: "Januar",
    72: "Februar",
    73: "Marts",
}


# =============================================================================
# PROCESS
# =============================================================================
def process(orchestrator_connection: OrchestratorConnection, queue_element: QueueElement | None = None) -> None:
    orchestrator_connection.log_trace("Running process.")

    brugaarhus_api_cred = orchestrator_connection.get_credential("BrugAarhusAPI")
    base_url = brugaarhus_api_cred.username
    token = brugaarhus_api_cred.password
    headers = {
        "Authorization": token,
        "Cookie": "dp_last_lang=da",
    }

    # SQL Connection
    sql_server = orchestrator_connection.get_constant("SqlServer")
    conn_string = (
        "DRIVER={SQL Server};"
        f"SERVER={sql_server.value};"
        "DATABASE=PYORCHESTRATOR;"
        "Trusted_Connection=yes;"
    )
    conn = pyodbc.connect(conn_string)
    cursor = conn.cursor()

    # -------------------------------
    # Fetch. A tilladelse enters kassen when its Workflow is "Ansøgning
    # godkendt" in the udeservering department — every type, specials too.
    # Once in kassen it stays, whatever happens to the status in Deskpro, and
    # its data (slutdato, m2, …) keeps flowing to the Ny lines. So tilladelser
    # already in kassen that still matter, but are no longer godkendt, are
    # fetched one by one. Kassen warns about the changed status.
    #
    # One query per ticket status (like sandbox.py): an approved tilladelse is
    # often a resolved or archived ticket.
    # -------------------------------
    tickets_by_id: dict[int, dict] = {}
    for status in fetch_ticket_statuses(base_url, headers):
        if status.startswith("hidden"):  # spam / deleted / awaiting validation
            continue
        query = (
            f"department={DEPARTMENT_ID}&status={status}"
            f"&ticket_field.{FIELD_WORKFLOW}={OPT_WORKFLOW_GODKENDT}"
        )
        for ticket in fetch_tickets(orchestrator_connection, base_url, headers, query):
            tickets_by_id[ticket["id"]] = ticket
    godkendt_count = len(tickets_by_id)

    for deskpro_id in kassen_ids_to_follow(cursor):
        if deskpro_id in tickets_by_id:
            continue
        ticket = fetch_ticket(orchestrator_connection, base_url, headers, deskpro_id)
        if ticket:
            tickets_by_id[deskpro_id] = ticket

    all_data = list(tickets_by_id.values())
    orchestrator_connection.log_info(
        f"Fetched {godkendt_count} godkendte tickets + "
        f"{len(all_data) - godkendt_count} already in kassen that are no longer godkendt."
    )

    # -------------------------------
    # Upsert every tilladelse. New rows get KassenStatus "Afventer" from the
    # column default; kassen's own approval is never overwritten here.
    # -------------------------------
    for ticket in all_data:
        cursor.execute(MERGE_SQL, tilladelse_params(ticket))
    conn.commit()
    cursor.close()
    conn.close()
    orchestrator_connection.log_info(f"Synced {len(all_data)} tilladelser from Deskpro.")

    # -------------------------------
    # Fakturalinjer: kassen's job. Committed above, so kassen sees the data.
    # -------------------------------
    opdater_fakturalinjer_i_kassen(orchestrator_connection, [t["id"] for t in all_data])


MERGE_SQL = """
    MERGE [dbo].[BrugAarhus_Udeservering] AS target
    USING (
        SELECT
            ? AS Id,
            ? AS Firmanavn,
            ? AS Adresse,
            ? AS CVR,
            ? AS Att,
            ? AS Serveringszone,
            ? AS Udeserveringstype,
            ? AS UdeserveringstypeOptionId,
            ? AS Serveringsareal,
            ? AS Facadelaengde,
            ? AS GaeldendeFra,
            ? AS GaeldendeTilOgMed,
            ? AS Sommersaeson,
            ? AS Vintermaaneder,
            ? AS Workflow,
            ? AS WorkflowOptionId,
            ? AS Ansogningsdato
    ) AS source
    ON (target.Id = source.Id)

    WHEN MATCHED THEN
        UPDATE SET
            Firmanavn                 = source.Firmanavn,
            Adresse                   = source.Adresse,
            CVR                       = source.CVR,
            Att                       = source.Att,
            Serveringszone            = source.Serveringszone,
            Udeserveringstype         = source.Udeserveringstype,
            UdeserveringstypeOptionId = source.UdeserveringstypeOptionId,
            Serveringsareal           = source.Serveringsareal,
            Facadelaengde             = source.Facadelaengde,
            GaeldendeFra              = source.GaeldendeFra,
            GaeldendeTilOgMed         = source.GaeldendeTilOgMed,
            Sommersaeson              = source.Sommersaeson,
            Vintermaaneder            = source.Vintermaaneder,
            Workflow                  = source.Workflow,
            WorkflowOptionId          = source.WorkflowOptionId,
            Ansogningsdato            = source.Ansogningsdato

    WHEN NOT MATCHED THEN
        INSERT (
            Id, Firmanavn, Adresse, CVR, Att,
            Serveringszone, Udeserveringstype, UdeserveringstypeOptionId,
            Serveringsareal, Facadelaengde,
            GaeldendeFra, GaeldendeTilOgMed,
            Sommersaeson, Vintermaaneder,
            Workflow, WorkflowOptionId,
            Ansogningsdato
        )
        VALUES (
            source.Id, source.Firmanavn, source.Adresse, source.CVR, source.Att,
            source.Serveringszone, source.Udeserveringstype, source.UdeserveringstypeOptionId,
            source.Serveringsareal, source.Facadelaengde,
            source.GaeldendeFra, source.GaeldendeTilOgMed,
            source.Sommersaeson, source.Vintermaaneder,
            source.Workflow, source.WorkflowOptionId,
            source.Ansogningsdato
        );
"""


def tilladelse_params(ticket: dict) -> tuple:
    """The MERGE parameters for one Deskpro ticket, in MERGE_SQL's order."""
    fields = ticket.get("fields", {}) or {}

    type_id = get_type_id(fields)
    type_title = safe_get_first_detail_title(fields, FIELD_UDESERVERINGSTYPE) or TYPE_FALLBACK_TITLE[type_id]

    startdato = parse_deskpro_date(safe_get_value(fields, FIELD_STARTDATO))
    slutdato = parse_deskpro_date(safe_get_value(fields, FIELD_SLUTDATO))
    ansogningsdato = parse_deskpro_dt(ticket.get("date_created"))

    # Season mirrors — kassen bills from these.
    sommersaeson_text, vintermaaneder_text = compute_saeson_text(type_id, fields)

    return (
        ticket.get("id"),
        safe_get_value(fields, FIELD_FIRMANAVN),
        safe_get_value(fields, FIELD_ADRESSE),
        safe_get_value(fields, FIELD_CVR),
        ticket.get("_att_name"),
        safe_get_first_detail_title(fields, FIELD_ZONE),
        type_title,
        type_id,
        safe_get_value(fields, FIELD_SERVERINGSAREAL),
        safe_get_value(fields, FIELD_FACADELAENGDE),
        startdato.strftime("%Y-%m-%d") if startdato else None,
        slutdato.strftime("%Y-%m-%d") if slutdato else None,
        sommersaeson_text,
        vintermaaneder_text,
        # Mirrored so kassen can warn when a tilladelse it still bills has
        # left "Ansøgning godkendt" in Deskpro.
        safe_get_first_detail_title(fields, FIELD_WORKFLOW),
        safe_get_single_select_id(fields, FIELD_WORKFLOW),
        ansogningsdato.strftime("%Y-%m-%d %H:%M:%S") if ansogningsdato else None,
    )


# =============================================================================
# KASSEN
# =============================================================================
def opdater_fakturalinjer_i_kassen(orchestrator_connection, ids: list[int]) -> None:
    """Ask BrugAarhus Kassen to bring the fakturalinjer of these tilladelser up
    to date. Credential BrugAarhusKassenAPI: username = kassen's base URL,
    password = the API key (BRUGAARHUS_ROBOT_API_KEY on the IIS site).

    A failure is logged, not raised: the Deskpro data is already saved, and the
    lines are brought up to date by kassen itself, or on the next run."""
    if not ids:
        return
    cred = orchestrator_connection.get_credential("BrugAarhusKassenAPI")
    url = f"{cred.username.rstrip('/')}/udeservering/api/fakturalinjer/dan"
    headers = {"X-API-Key": cred.password}

    totals = {"tilladelser": 0, "dannet": 0, "opdateret": 0, "slettet": 0}
    failed = 0
    ids = sorted(set(ids))
    for i in range(0, len(ids), KASSEN_CHUNK):
        chunk = ids[i:i + KASSEN_CHUNK]
        try:
            resp = requests.post(url, json={"ids": chunk}, headers=headers, timeout=120)
        except requests.RequestException as exc:
            failed += len(chunk)
            orchestrator_connection.log_error(f"Kassen unreachable for fakturalinjer: {exc}")
            continue
        if resp.status_code != 200:
            failed += len(chunk)
            orchestrator_connection.log_error(
                f"Kassen refused fakturalinjer ({resp.status_code}): {resp.text[:300]}"
            )
            continue
        body = resp.json()
        for key in totals:
            totals[key] += int(body.get(key) or 0)

    orchestrator_connection.log_info(
        "Fakturalinjer via kassen: "
        f"{totals['tilladelser']} tilladelser, {totals['dannet']} created, "
        f"{totals['opdateret']} updated, {totals['slettet']} deleted"
        + (f", {failed} tilladelser NOT updated (see errors)" if failed else "")
    )


# =============================================================================
# FETCH
# =============================================================================
def fetch_ticket_statuses(base_url: str, headers: dict) -> list[str]:
    """Ticket statuses (awaiting_agent, resolved, archived, …) via the old
    statuses endpoint, as in sandbox.py."""
    resp = requests.get(f"{base_url}/api/v2/20190110/ticket_statuses", headers=headers, timeout=60)
    resp.raise_for_status()
    return [str(s).strip() for s in (resp.json().get("data") or []) if str(s).strip()]


def _stamp_att(tickets: list[dict], payload: dict) -> None:
    """Applicant name from include=person (linked.person.<id>.name) → Att."""
    linked = payload.get("linked", {}) if isinstance(payload.get("linked"), dict) else {}
    person_map = linked.get("person", {}) if isinstance(linked.get("person"), dict) else {}
    for ticket in tickets:
        person = person_map.get(str(ticket.get("person")))
        if isinstance(person, dict):
            ticket["_att_name"] = person.get("name") or person.get("display_name")


def fetch_tickets(orchestrator_connection, base_url: str, headers: dict, filter_query: str) -> list[dict]:
    """All pages of /api/v2/tickets?<filter_query>, each ticket stamped with
    its applicant name."""
    api_url = f"{base_url}/api/v2/tickets?{filter_query}&include=person&count=100"
    out: list[dict] = []
    page = 1
    while True:
        resp = requests.get(f"{api_url}&page={page}", headers=headers, timeout=60)
        if resp.status_code != 200:
            orchestrator_connection.log_error(
                f"API request failed ({filter_query}): {resp.status_code} {resp.text[:500]}"
            )
            break

        payload = resp.json()
        data = payload.get("data", [])
        pagination = (payload.get("meta", {}) or {}).get("pagination", {})
        _stamp_att(data, payload)

        if not data:
            break
        out.extend(data)
        if pagination.get("current_page", 1) >= pagination.get("total_pages", 1):
            break
        page += 1
    return out


def fetch_ticket(orchestrator_connection, base_url: str, headers: dict, ticket_id: int) -> dict | None:
    """One ticket via /api/v2/tickets/{id}, stamped with its applicant name.
    None (and logged) if it can't be fetched, e.g. deleted — kassen then
    keeps the data it has."""
    resp = requests.get(f"{base_url}/api/v2/tickets/{ticket_id}?include=person", headers=headers, timeout=60)
    if resp.status_code != 200:
        orchestrator_connection.log_error(
            f"Could not fetch ticket {ticket_id}: {resp.status_code} {resp.text[:300]}"
        )
        return None
    payload = resp.json()
    ticket = payload.get("data")
    if not isinstance(ticket, dict):
        return None
    _stamp_att([ticket], payload)
    return ticket


def kassen_ids_to_follow(cursor) -> list[int]:
    """Tilladelser already in kassen that still matter: active (no slutdato,
    or one in this month or later) or with Ny lines not yet approved. Ended
    tilladelser with only locked lines have nothing left to update."""
    cursor.execute(
        """
        SELECT u.Id
        FROM dbo.BrugAarhus_Udeservering u
        WHERE u.GaeldendeTilOgMed IS NULL
           OR u.GaeldendeTilOgMed >= DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1)
           OR EXISTS (
                SELECT 1 FROM dbo.BrugAarhus_Udeservering_Fakturalinjer f
                WHERE f.DeskproID = u.Id AND f.FakturaStatus = 'Ny'
           );
        """
    )
    return [int(r.Id) for r in cursor.fetchall()]


# =============================================================================
# FIELD MAPPING
# =============================================================================
def get_type_id(fields: dict) -> int:
    """Udeserveringstype option id. Missing/unknown → Alm. udeservering, the
    form's own default."""
    type_id = safe_get_single_select_id(fields, FIELD_UDESERVERINGSTYPE)
    return type_id if type_id in TYPE_FALLBACK_TITLE else OPT_TYPE_ALM


def compute_saeson_text(type_id: int, fields: dict) -> tuple[str | None, str | None]:
    """Human-readable mirrors of the Sommer/Vinter selections. Kassen bills
    from these, so keep the format: Sommersaeson "Ja" / "Nej" / None,
    Vintermaaneder Danish month names joined by ", " (Oktober → Marts order).
    Specialtilladelser are never billed: (None, None)."""
    if type_id == OPT_TYPE_SPECIAL:
        return None, None

    sommer_ids = safe_get_multi_select_ids(fields, FIELD_SOMMERSAESON)
    if OPT_SOMMER_JA in sommer_ids:
        sommer_text = "Ja"
    elif OPT_SOMMER_NEJ in sommer_ids:
        sommer_text = "Nej"
    else:
        sommer_text = None

    vinter_ids = safe_get_multi_select_ids(fields, FIELD_VINTERSAESON)
    # Sort by option-id so months come out in Okt→Nov→Dec→Jan→Feb→Marts order.
    winter_names = [WINTER_OPT_TO_NAME[oid] for oid in sorted(vinter_ids) if oid in WINTER_OPT_TO_NAME]
    vinter_text = ", ".join(winter_names) if winter_names else None

    return sommer_text, vinter_text


# =============================================================================
# HELPERS
# =============================================================================
def safe_get_value(field_dict: dict, key: str, default=None):
    try:
        return field_dict[key].get("value", default)
    except Exception:
        return default


def safe_get_first_detail_title(field_dict: dict, key: str, default=None):
    try:
        detail = field_dict[key].get("detail", {})
        if isinstance(detail, dict) and detail:
            return list(detail.values())[0].get("title", default)
    except Exception:
        pass
    return default


def safe_get_single_select_id(field_dict: dict, key: str, default=None):
    try:
        v = field_dict[key].get("value")
        if isinstance(v, list) and v:
            return int(v[0])
        if isinstance(v, (int, str)) and str(v).strip().isdigit():
            return int(v)
    except Exception:
        pass
    return default


def safe_get_multi_select_ids(field_dict: dict, key: str) -> list[int]:
    """Selected option IDs for radio/checkbox/multichoice fields.
    Handles both single (value = [id]) and multi (value = [id, id, …])."""
    try:
        v = field_dict[key].get("value")
        if isinstance(v, list):
            return [int(x) for x in v if isinstance(x, int) or (isinstance(x, str) and str(x).isdigit())]
        if isinstance(v, (int, str)) and str(v).strip().isdigit():
            return [int(v)]
    except Exception:
        pass
    return []


def parse_deskpro_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S%z")
        dt_cph = dt.astimezone(ZoneInfo("Europe/Copenhagen"))
        return dt_cph.replace(tzinfo=None)
    except Exception:
        return None


def parse_deskpro_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
            return datetime.strptime(raw, "%Y-%m-%d").date()
        dt = parse_deskpro_dt(raw)
        return dt.date() if dt else None
    except Exception:
        return None

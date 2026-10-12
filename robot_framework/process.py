"""This module contains the main process of the robot."""

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

# Sæson — drives which months a tilladelse actually gets billed in. Which
# types show which season field is decided by the Deskpro form, not here.
FIELD_SOMMERSAESON = "64"          # Radio: Ja (65) → all six summer months
FIELD_VINTERSAESON = "67"          # Checkbox multi: each selected month is billable

# Deskpro department of the udeservering form ("TEST - Udeservering").
DEPARTMENT_ID = 23


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

# Sommersæson Ja → all six months of April through September
OPT_SOMMER_JA = 65
OPT_SOMMER_NEJ = 66
SUMMER_MONTHS = (4, 5, 6, 7, 8, 9)

# Vintersæson option → month number. The option-id order (68→73) is the
# natural Danish winter-season order (Oktober → Marts).
WINTER_OPT_TO_MONTH = {
    68: 10,  # Oktober
    69: 11,  # November
    70: 12,  # December
    71: 1,   # Januar
    72: 2,   # Februar
    73: 3,   # Marts
}
WINTER_OPT_TO_NAME = {
    68: "Oktober",
    69: "November",
    70: "December",
    71: "Januar",
    72: "Februar",
    73: "Marts",
}


# How many months ahead of "now" we generate fakturalinjer for when the
# tilladelse is open-ended (no slutdato set). Past months are always
# back-filled to the startdato regardless.
MONTHS_AHEAD = 6

MONTH_NUM_TO_NAME = {
    1: "Januar", 2: "Februar", 3: "Marts", 4: "April",
    5: "Maj", 6: "Juni", 7: "Juli", 8: "August",
    9: "September", 10: "Oktober", 11: "November", 12: "December",
}
MONTH_NAME_TO_NUM = {v: k for k, v in MONTH_NUM_TO_NAME.items()}


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
    # Single loop: per ticket, upsert the Udeservering row AND drive the
    # fakturalinje generation. The sæson selection lives only in Deskpro —
    # we recompute it every refresh.
    # -------------------------------
    now_cph = datetime.now(ZoneInfo("Europe/Copenhagen"))
    horizon = add_months(now_cph.year, now_cph.month, MONTHS_AHEAD - 1)

    inserted_count = 0
    updated_count = 0
    deleted_count = 0
    skipped_locked = 0
    skipped_invalid = 0
    special_count = 0

    for ticket in all_data:
        fields = ticket.get("fields", {}) or {}

        deskpro_id = ticket.get("id")

        firmanavn = safe_get_value(fields, FIELD_FIRMANAVN)
        adresse = safe_get_value(fields, FIELD_ADRESSE)
        cvr = safe_get_value(fields, FIELD_CVR)
        att = ticket.get("_att_name")

        serveringszone = safe_get_first_detail_title(fields, FIELD_ZONE)

        type_id = get_type_id(fields)
        type_title = safe_get_first_detail_title(fields, FIELD_UDESERVERINGSTYPE) or TYPE_FALLBACK_TITLE[type_id]

        # Mirrored so kassen can warn when a tilladelse it still bills has left
        # "Ansøgning godkendt" in Deskpro. Billing itself ignores it.
        workflow_id = safe_get_single_select_id(fields, FIELD_WORKFLOW)
        workflow_title = safe_get_first_detail_title(fields, FIELD_WORKFLOW)

        serveringsareal = safe_get_value(fields, FIELD_SERVERINGSAREAL)
        facadelaengde = safe_get_value(fields, FIELD_FACADELAENGDE)

        startdato = parse_deskpro_date(safe_get_value(fields, FIELD_STARTDATO))
        slutdato = parse_deskpro_date(safe_get_value(fields, FIELD_SLUTDATO))

        # Sæson billable-month set — computed fresh from the ticket every run.
        # Empty set (nothing selected, or a specialtilladelse) means *no*
        # fakturalinjer. Kassen surfaces "mangler sæson" with a red warning.
        billable_months = compute_billable_months(type_id, fields)

        # Display-only mirrors persisted to BrugAarhus_Udeservering.
        sommersaeson_text, vintermaaneder_text = compute_saeson_text(type_id, fields)

        # Ticket created → Ansøgningsdato
        ansogningsdato = parse_deskpro_dt(ticket.get("date_created"))

        ansogningsdato_sql = ansogningsdato.strftime("%Y-%m-%d %H:%M:%S") if ansogningsdato else None
        gaeldende_fra_sql = startdato.strftime("%Y-%m-%d") if startdato else None
        gaeldende_til_sql = slutdato.strftime("%Y-%m-%d") if slutdato else None

        # -------- Upsert Udeservering --------
        cursor.execute(
            """
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
            """,
            (
                deskpro_id,
                firmanavn,
                adresse,
                cvr,
                att,
                serveringszone,
                type_title,
                type_id,
                serveringsareal,
                facadelaengde,
                gaeldende_fra_sql,
                gaeldende_til_sql,
                sommersaeson_text,
                vintermaaneder_text,
                workflow_title,
                workflow_id,
                ansogningsdato_sql,
            ),
        )

        if type_id == OPT_TYPE_SPECIAL:
            special_count += 1

        # -------- Generate / refresh fakturalinjer for this tilladelse --------
        if not startdato:
            skipped_invalid += 1
            continue

        wanted = billable_year_months(startdato, slutdato, horizon, billable_months)

        # Existing lines for this tilladelse, keyed by (year, month).
        cursor.execute(
            """
            SELECT FakturaLinjeID, FakturaMaaned, FakturaAar, FakturaStatus
            FROM dbo.BrugAarhus_Udeservering_Fakturalinjer
            WHERE DeskproID = ?;
            """,
            (deskpro_id,),
        )
        existing = {}
        for row in cursor.fetchall():
            m = MONTH_NAME_TO_NUM.get((row.FakturaMaaned or "").split(" ")[0])
            if m:
                existing[(int(row.FakturaAar), m)] = (row.FakturaLinjeID, row.FakturaStatus)

        # Ny lines that are no longer wanted: out of sæson, past a (new)
        # slutdato, or the type became special.
        # Locked statuses (TilFakturering / Faktureret / FakturerIkke) stay.
        for ym, (linje_id, status) in existing.items():
            if ym in wanted:
                continue
            if status == "Ny":
                cursor.execute(
                    "DELETE FROM dbo.BrugAarhus_Udeservering_Fakturalinjer "
                    "WHERE FakturaLinjeID = ? AND FakturaStatus = 'Ny';",
                    (linje_id,),
                )
                deleted_count += 1

        for (y, m) in wanted:
            month_name = MONTH_NUM_TO_NAME[m]

            if (y, m) in existing:
                linje_id, status = existing[(y, m)]
                if status != "Ny":
                    skipped_locked += 1
                    continue
                cursor.execute(
                    """
                    UPDATE dbo.BrugAarhus_Udeservering_Fakturalinjer
                    SET Firmanavn                 = ?,
                        Adresse                   = ?,
                        CVR                       = ?,
                        Att                       = ?,
                        Serveringszone            = ?,
                        Udeserveringstype         = ?,
                        UdeserveringstypeOptionId = ?,
                        Serveringsareal           = ?,
                        Facadelaengde             = ?,
                        Ansogningsdato            = ?
                    WHERE FakturaLinjeID = ?
                      AND FakturaStatus = 'Ny';
                    """,
                    (
                        firmanavn,
                        adresse,
                        cvr,
                        att,
                        serveringszone,
                        type_title,
                        type_id,
                        serveringsareal,
                        facadelaengde,
                        ansogningsdato_sql,
                        linje_id,
                    ),
                )
                updated_count += 1
                continue

            cursor.execute(
                """
                INSERT INTO dbo.BrugAarhus_Udeservering_Fakturalinjer (
                    DeskproID,
                    FakturaMaaned,
                    FakturaAar,
                    FakturaDatoSort,
                    Firmanavn,
                    Adresse,
                    CVR,
                    Att,
                    Serveringszone,
                    Udeserveringstype,
                    UdeserveringstypeOptionId,
                    Serveringsareal,
                    Facadelaengde,
                    Ansogningsdato,
                    FakturaStatus
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Ny');
                """,
                (
                    deskpro_id,
                    month_name,
                    y,
                    datetime(y, m, 1),
                    firmanavn,
                    adresse,
                    cvr,
                    att,
                    serveringszone,
                    type_title,
                    type_id,
                    serveringsareal,
                    facadelaengde,
                    ansogningsdato_sql,
                ),
            )
            inserted_count += 1

    conn.commit()

    orchestrator_connection.log_info(
        "Refresh complete. "
        f"Inserted: {inserted_count}, "
        f"Updated Ny: {updated_count}, "
        f"Deleted (no longer billable Ny): {deleted_count}, "
        f"Skipped (locked): {skipped_locked}, "
        f"Skipped (no startdato): {skipped_invalid}, "
        f"Specialtilladelser (no lines): {special_count}"
    )

    cursor.close()
    conn.close()


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
# BILLING RULES
# =============================================================================
def get_type_id(fields: dict) -> int:
    """Udeserveringstype option id. Missing/unknown → Alm. udeservering, the
    form's own default."""
    type_id = safe_get_single_select_id(fields, FIELD_UDESERVERINGSTYPE)
    return type_id if type_id in TYPE_FALLBACK_TITLE else OPT_TYPE_ALM


def compute_billable_months(type_id: int, fields: dict) -> set[int]:
    """Month numbers (1–12) the tilladelse is billed in: Sommersæson Ja →
    Apr–Sep, plus each selected winter month. The same for every type except
    specialtilladelse, which is never billed (empty set). Rules such as
    "parklet only in summer" live in the Deskpro form and the guidelines, so
    they can change without touching this code."""
    if type_id == OPT_TYPE_SPECIAL:
        return set()

    months: set[int] = set()
    if OPT_SOMMER_JA in safe_get_multi_select_ids(fields, FIELD_SOMMERSAESON):
        months.update(SUMMER_MONTHS)
    vinter_ids = safe_get_multi_select_ids(fields, FIELD_VINTERSAESON)
    months.update(WINTER_OPT_TO_MONTH[oid] for oid in vinter_ids if oid in WINTER_OPT_TO_MONTH)
    return months


def billable_year_months(
    startdato: date,
    slutdato: date | None,
    horizon: tuple[int, int],
    billable_months: set[int],
) -> list[tuple[int, int]]:
    """(year, month) pairs that should have a fakturalinje: every billable
    month from the start month through the end month (both billed in full),
    capped at the horizon for open-ended (fortløbende) tilladelser."""
    gen_from = (startdato.year, startdato.month)
    gen_to = horizon
    if slutdato:
        gen_to = min(gen_to, (slutdato.year, slutdato.month))
    return [ym for ym in iter_year_months(gen_from, gen_to) if ym[1] in billable_months]


def compute_saeson_text(type_id: int, fields: dict) -> tuple[str | None, str | None]:
    """Return human-readable mirrors of the Sommer/Vinter selections so
    Kassen can show them on the tilladelse without re-reading Deskpro.

    Sommersaeson    : "Ja" / "Nej" / None (unanswered, or specialtilladelse)
    Vintermaaneder  : "Oktober, November, December" / None (none selected,
                      or specialtilladelse)
    """
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
def add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = (year * 12 + (month - 1)) + delta
    return (idx // 12, idx % 12 + 1)


def iter_year_months(start: tuple[int, int], end: tuple[int, int]):
    y, m = start
    ey, em = end
    while (y, m) <= (ey, em):
        yield (y, m)
        m += 1
        if m > 12:
            m = 1
            y += 1


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

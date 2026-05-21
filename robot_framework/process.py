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
FIELD_FIRMANAVN = "55"             # Cafeens / restaurantens navn
FIELD_ADRESSE = "255"              # Cafeens / restaurantens adresse
FIELD_GEO = "268"                  # Cafeens / restaurantens adresse (geo)
FIELD_CVR = "1258"                 # TEST Udeservering/Vareudstilling — CVR (number)

# Sagsdata
FIELD_ZONE = "1216"                # TEST Udeservering — Zone (1 / 2)
FIELD_LOKATION = "1192"            # TEST Udeservering — Hvor ønskes udeservering?
FIELD_SERVERINGSAREAL = "1196"
FIELD_FACADELAENGDE = "1210"

# Periode
FIELD_GAELDENDE_FRA = "1291"
FIELD_GAELDENDE_TIL_OG_MED = "1292"
FIELD_OPSIGELSE = "1318"           # Slutdato hvis tilladelse opsiges; vinder over 1292

# Sæson — drives which months a tilladelse actually gets billed in
FIELD_SOMMERSAESON = "64"          # Radio: Ja (65) → bill all 6 summer months
FIELD_VINTERSAESON = "67"          # Checkbox multi: each selected month is billable

# Fakturering trigger from Deskpro side
FIELD_FAKTURERINGSSTATUS = "1228"  # Send (1229) / Send ikke (1230)


# =============================================================================
# OPTION IDS
# =============================================================================
# Lokation (field 1192)
OPT_LOKATION_FACADE = 1193     # "Facade og nærliggende areal"
OPT_LOKATION_TORV = 1194       # "Nærliggende torv/plads"
OPT_LOKATION_PARKLET = 1195    # "Parklet"

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

OPT_FAKTURERING_SEND = 1229


# How many months ahead of "now" we generate fakturalinjer for when the
# tilladelse is open-ended (no slutdato set). Past months are always
# back-filled to gaeldende_fra regardless.
MONTHS_AHEAD = 6

MONTH_NUM_TO_NAME = {
    1: "Januar", 2: "Februar", 3: "Marts", 4: "April",
    5: "Maj", 6: "Juni", 7: "Juli", 8: "August",
    9: "September", 10: "Oktober", 11: "November", 12: "December",
}


# =============================================================================
# PROCESS
# =============================================================================
def process(orchestrator_connection: OrchestratorConnection, queue_element: QueueElement | None = None) -> None:
    orchestrator_connection.log_trace("Running process.")

    brugaarhus_api_cred = orchestrator_connection.get_credential("BrugAarhusAPI")
    base_url = brugaarhus_api_cred.username
    token = brugaarhus_api_cred.password

    # Only fetch tickets explicitly marked "Send til fakturering".
    # include=person → linked.person.<id>.name resolves to the Att applicant name.
    api_url = (
        f"{base_url}/api/v2/tickets"
        f"?ticket_field.{FIELD_FAKTURERINGSSTATUS}={OPT_FAKTURERING_SEND}"
        f"&include=person"
        f"&count=100"
    )
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
    # Fetch all pages, stamping each ticket with its applicant name.
    # -------------------------------
    all_data: list[dict] = []
    page = 1
    while True:
        resp = requests.get(f"{api_url}&page={page}", headers=headers, timeout=60)
        if resp.status_code != 200:
            orchestrator_connection.log_error(
                f"API request failed: {resp.status_code} {resp.text[:500]}"
            )
            break

        payload = resp.json()
        data = payload.get("data", [])
        meta = payload.get("meta", {})
        pagination = meta.get("pagination", {})
        linked = payload.get("linked", {}) if isinstance(payload.get("linked"), dict) else {}
        person_map = linked.get("person", {}) if isinstance(linked.get("person"), dict) else {}

        for ticket in data:
            pid = ticket.get("person")
            if pid is None:
                continue
            person = person_map.get(str(pid))
            if isinstance(person, dict):
                ticket["_att_name"] = person.get("name") or person.get("display_name")

        if not data:
            break
        all_data.extend(data)
        if pagination.get("current_page", 1) >= pagination.get("total_pages", 1):
            break
        page += 1

    orchestrator_connection.log_info(f"Fetched {len(all_data)} tickets from API.")

    # -------------------------------
    # Single loop: per ticket, upsert the Udeservering row AND drive the
    # fakturalinje generation. The sæson selection lives only in Deskpro —
    # we recompute it every refresh, no need to persist it in our DB.
    # -------------------------------
    now_cph = datetime.now(ZoneInfo("Europe/Copenhagen"))
    horizon_year, horizon_month = add_months(now_cph.year, now_cph.month, MONTHS_AHEAD - 1)

    inserted_count = 0
    updated_count = 0
    deleted_count = 0
    skipped_locked = 0
    skipped_invalid = 0

    for ticket in all_data:
        fields = ticket.get("fields", {}) or {}

        deskpro_id = ticket.get("id")

        firmanavn = safe_get_value(fields, FIELD_FIRMANAVN)
        adresse = safe_get_value(fields, FIELD_ADRESSE)
        cvr = safe_get_value(fields, FIELD_CVR)
        geo = safe_get_value(fields, FIELD_GEO)
        att = ticket.get("_att_name")

        serveringszone = safe_get_first_detail_title(fields, FIELD_ZONE)

        lokation_option_id = safe_get_single_select_id(fields, FIELD_LOKATION)
        lokation_title = safe_get_first_detail_title(fields, FIELD_LOKATION)

        facadelaengde = safe_get_value(fields, FIELD_FACADELAENGDE)
        serveringsareal = safe_get_value(fields, FIELD_SERVERINGSAREAL)

        gaeldende_fra = parse_deskpro_date(safe_get_value(fields, FIELD_GAELDENDE_FRA))
        planlagt_til = parse_deskpro_date(safe_get_value(fields, FIELD_GAELDENDE_TIL_OG_MED))
        opsigelse = parse_deskpro_date(safe_get_value(fields, FIELD_OPSIGELSE))
        effective_til = opsigelse or planlagt_til

        # Sæson billable-month set — computed fresh from the ticket every run.
        # If nothing is selected (neither Sommer=Ja nor any winter month), the
        # set is empty and *no* fakturalinjer will be generated for this
        # tilladelse. Any existing Ny rows in the window are deleted as
        # out-of-sæson. Kassen surfaces this state with a red warning so the
        # sagsbehandler knows to fix the tilladelse in Deskpro.
        billable_months = compute_billable_months(fields)

        # Display-only mirrors persisted to BrugAarhus_Udeservering.
        sommersaeson_text, vintermaaneder_text = compute_saeson_text(fields)

        # Ticket created → Ansøgningsdato
        raw_created = ticket.get("date_created")
        ansogningsdato = parse_deskpro_dt(raw_created)

        ansogningsdato_sql = ansogningsdato.strftime("%Y-%m-%d %H:%M:%S") if ansogningsdato else None
        gaeldende_fra_sql = gaeldende_fra.strftime("%Y-%m-%d") if gaeldende_fra else None
        gaeldende_til_sql = effective_til.strftime("%Y-%m-%d") if effective_til else None

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
                    ? AS Geo,
                    ? AS Serveringszone,
                    ? AS Lokation,
                    ? AS LokationOptionId,
                    ? AS Serveringsareal,
                    ? AS Facadelaengde,
                    ? AS GaeldendeFra,
                    ? AS GaeldendeTilOgMed,
                    ? AS Sommersaeson,
                    ? AS Vintermaaneder,
                    ? AS Ansogningsdato
            ) AS source
            ON (target.Id = source.Id)

            WHEN MATCHED THEN
                UPDATE SET
                    Firmanavn         = source.Firmanavn,
                    Adresse           = source.Adresse,
                    CVR               = source.CVR,
                    Att               = source.Att,
                    Geo               = source.Geo,
                    Serveringszone    = source.Serveringszone,
                    Lokation          = source.Lokation,
                    LokationOptionId  = source.LokationOptionId,
                    Serveringsareal   = source.Serveringsareal,
                    Facadelaengde     = source.Facadelaengde,
                    GaeldendeFra      = source.GaeldendeFra,
                    GaeldendeTilOgMed = source.GaeldendeTilOgMed,
                    Sommersaeson      = source.Sommersaeson,
                    Vintermaaneder    = source.Vintermaaneder,
                    Ansogningsdato    = source.Ansogningsdato

            WHEN NOT MATCHED THEN
                INSERT (
                    Id, Firmanavn, Adresse, CVR, Att, Geo,
                    Serveringszone, Lokation, LokationOptionId,
                    Serveringsareal, Facadelaengde,
                    GaeldendeFra, GaeldendeTilOgMed,
                    Sommersaeson, Vintermaaneder,
                    Ansogningsdato
                )
                VALUES (
                    source.Id, source.Firmanavn, source.Adresse, source.CVR, source.Att, source.Geo,
                    source.Serveringszone, source.Lokation, source.LokationOptionId,
                    source.Serveringsareal, source.Facadelaengde,
                    source.GaeldendeFra, source.GaeldendeTilOgMed,
                    source.Sommersaeson, source.Vintermaaneder,
                    source.Ansogningsdato
                );
            """,
            (
                deskpro_id,
                firmanavn,
                adresse,
                cvr,
                att,
                geo,
                serveringszone,
                lokation_title,
                lokation_option_id,
                serveringsareal,
                facadelaengde,
                gaeldende_fra_sql,
                gaeldende_til_sql,
                sommersaeson_text,
                vintermaaneder_text,
                ansogningsdato_sql,
            ),
        )

        # -------- Generate / refresh fakturalinjer for this tilladelse --------
        if not gaeldende_fra:
            skipped_invalid += 1
            continue

        gen_from = (gaeldende_fra.year, gaeldende_fra.month)
        gen_to = (horizon_year, horizon_month)
        if effective_til:
            til_pair = (effective_til.year, effective_til.month)
            if til_pair < gen_to:
                gen_to = til_pair
        if gen_to < gen_from:
            continue

        # Parklet has no areal — charged differently.
        faktura_areal = None if lokation_option_id == OPT_LOKATION_PARKLET else serveringsareal

        for (y, m) in iter_year_months(gen_from, gen_to):
            month_name = MONTH_NUM_TO_NAME[m]
            faktura_date_sort = datetime(y, m, 1)

            cursor.execute(
                """
                SELECT FakturaStatus
                FROM dbo.BrugAarhus_Udeservering_Fakturalinjer
                WHERE DeskproID = ?
                  AND FakturaMaaned = ?
                  AND FakturaAar = ?;
                """,
                (deskpro_id, month_name, y),
            )
            existing = cursor.fetchone()

            # Month not in current sæson selection: drop any Ny line; leave
            # locked statuses (TilFakturering / Faktureret / FakturerIkke) alone.
            if m not in billable_months:
                if existing and existing.FakturaStatus == "Ny":
                    cursor.execute(
                        """
                        DELETE FROM dbo.BrugAarhus_Udeservering_Fakturalinjer
                        WHERE DeskproID = ?
                          AND FakturaMaaned = ?
                          AND FakturaAar = ?
                          AND FakturaStatus = 'Ny';
                        """,
                        (deskpro_id, month_name, y),
                    )
                    deleted_count += 1
                elif existing:
                    skipped_locked += 1
                continue

            if existing:
                if existing.FakturaStatus == "Ny":
                    cursor.execute(
                        """
                        UPDATE dbo.BrugAarhus_Udeservering_Fakturalinjer
                        SET Firmanavn       = ?,
                            Adresse         = ?,
                            CVR             = ?,
                            Att             = ?,
                            Geo             = ?,
                            Serveringszone  = ?,
                            Lokation        = ?,
                            Serveringsareal = ?,
                            Facadelaengde   = ?,
                            Ansogningsdato  = ?
                        WHERE DeskproID    = ?
                          AND FakturaMaaned = ?
                          AND FakturaAar    = ?;
                        """,
                        (
                            firmanavn,
                            adresse,
                            cvr,
                            att,
                            geo,
                            serveringszone,
                            lokation_title,
                            faktura_areal,
                            facadelaengde,
                            ansogningsdato_sql,
                            deskpro_id,
                            month_name,
                            y,
                        ),
                    )
                    updated_count += 1
                else:
                    skipped_locked += 1
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
                    Geo,
                    Serveringszone,
                    Lokation,
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
                    faktura_date_sort,
                    firmanavn,
                    adresse,
                    cvr,
                    att,
                    geo,
                    serveringszone,
                    lokation_title,
                    faktura_areal,
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
        f"Deleted (out-of-sæson Ny): {deleted_count}, "
        f"Skipped (locked): {skipped_locked}, "
        f"Skipped (invalid): {skipped_invalid}"
    )

    cursor.close()
    conn.close()


# =============================================================================
# HELPERS
# =============================================================================
def compute_billable_months(fields: dict) -> set[int]:
    """Union of Sommersæson + Vintersæson selections from Deskpro,
    as a set of integer month numbers (1–12).
    Returns empty set if neither field has any selection (caller treats
    this as legacy / unanswered and falls back to all 12 months)."""
    sommer_ids = safe_get_multi_select_ids(fields, FIELD_SOMMERSAESON)
    vinter_ids = safe_get_multi_select_ids(fields, FIELD_VINTERSAESON)

    months: set[int] = set()
    if OPT_SOMMER_JA in sommer_ids:
        months.update(SUMMER_MONTHS)
    months.update(WINTER_OPT_TO_MONTH[oid] for oid in vinter_ids if oid in WINTER_OPT_TO_MONTH)
    return months


def compute_saeson_text(fields: dict) -> tuple[str | None, str | None]:
    """Return human-readable mirrors of the Sommer/Vinter selections so
    Kassen can show them on the tilladelse without re-reading Deskpro.

    Sommersaeson    : "Ja" / "Nej" / None (unanswered)
    Vintermaaneder  : "Oktober, November, December" / None (none selected)
    """
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


def ensure_date(val) -> date | None:
    if val is None:
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, str):
        s = val.strip()
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except Exception:
            return None
    return None

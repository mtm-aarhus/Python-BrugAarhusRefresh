"""This module contains the main process of the robot."""

from OpenOrchestrator.orchestrator_connection.connection import OrchestratorConnection
from OpenOrchestrator.database.queues import QueueElement
import requests
import pyodbc
from datetime import datetime, date
from zoneinfo import ZoneInfo


# -----------------------------
# DESKPRO FIELD IDS
# -----------------------------
# Stam-data
FIELD_FIRMANAVN = "55"             # Cafeens / restaurantens navn
FIELD_ADRESSE = "255"              # Cafeens / restaurantens adresse
FIELD_CVR = "1258"                 # CVR Nummer (TEST Udeservering/Vareudstilling)
FIELD_GEO = "268"                  # Cafeens / restaurantens adresse (geo)

# Sagsdata
FIELD_ZONE = "1216"                # Zone (single select)
FIELD_LOKATION = "1192"            # Hvor ønskes udeservering? (single select)
FIELD_SERVERINGSAREAL = "1196"     # Areal i m²
FIELD_FACADELAENGDE = "1210"       # Facadelængde i meter

# Workflow / styring
FIELD_WORKFLOW = "1147"

# Periode
# - Gældende fra (1291): start.
# - Gældende til og med (1292): planlagt slutdato (tidsbegrænset).
# - Opsigelse (1318): vinder over 1292 hvis sat.
# Begge slutdato-felter kollapses til én værdi i Kassen (GaeldendeTilOgMed).
FIELD_GAELDENDE_FRA = "1291"
FIELD_GAELDENDE_TIL_OG_MED = "1292"
FIELD_OPSIGELSE = "1318"

# Lokation option ids
OPT_LOKATION_FACADE = 1193
OPT_LOKATION_TORV = 1194
OPT_LOKATION_PARKLET = 1195

# Fakturering
FIELD_FAKTURERINGSSTATUS = "1228"  # Send til fakturering (1229) / Fakturer ikke (1230)


# How many months ahead of "now" to generate fakturalinjer for when the
# tilladelse is open-ended (no slutdato set on Deskpro). Past months are
# always generated back to gaeldende_fra regardless of this number.
MONTHS_AHEAD = 6


MONTH_NUM_TO_NAME = {
    1: "Januar", 2: "Februar", 3: "Marts", 4: "April",
    5: "Maj", 6: "Juni", 7: "Juli", 8: "August",
    9: "September", 10: "Oktober", 11: "November", 12: "December",
}


# -----------------------------
# PROCESS
# -----------------------------
def process(orchestrator_connection: OrchestratorConnection, queue_element: QueueElement | None = None) -> None:
    orchestrator_connection.log_trace("Running process.")

    brugaarhus_api_cred = orchestrator_connection.get_credential("BrugAarhusAPI")
    base_url = brugaarhus_api_cred.username
    token = brugaarhus_api_cred.password

    # Filter: only tickets marked "Send til fakturering" (option 1229 of field 1228).
    # include=person pulls the applicant in `linked.person` so we can store Att.
    api_url = (
        f"{base_url}/api/v2/tickets"
        f"?ticket_field.{FIELD_FAKTURERINGSSTATUS}=1229"
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
    # Fetch all pages of API data
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

        # Resolve each ticket's applicant name from the linked.person map and
        # stamp it onto the ticket as `_att_name` for downstream use.
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
    # Upsert applications into dbo.BrugAarhus_Udeservering
    # -------------------------------
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

        # Collapse the two Deskpro end-dates into one effective end-date for Kassen.
        # Opsigelse wins over planlagt slutdato.
        effective_til = opsigelse or planlagt_til

        # Ticket created
        raw_created = ticket.get("date_created")
        ansogningsdato = parse_deskpro_dt(raw_created)

        ansogningsdato_sql = ansogningsdato.strftime("%Y-%m-%d %H:%M:%S") if ansogningsdato else None
        gaeldende_fra_sql = gaeldende_fra.strftime("%Y-%m-%d") if gaeldende_fra else None
        gaeldende_til_sql = effective_til.strftime("%Y-%m-%d") if effective_til else None

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
                    ? AS Ansogningsdato
            ) AS source
            ON (target.Id = source.Id)

            WHEN MATCHED THEN
                UPDATE SET
                    Firmanavn = source.Firmanavn,
                    Adresse = source.Adresse,
                    CVR = source.CVR,
                    Att = source.Att,
                    Geo = source.Geo,
                    Serveringszone = source.Serveringszone,
                    Lokation = source.Lokation,
                    LokationOptionId = source.LokationOptionId,
                    Serveringsareal = source.Serveringsareal,
                    Facadelaengde = source.Facadelaengde,
                    GaeldendeFra = source.GaeldendeFra,
                    GaeldendeTilOgMed = source.GaeldendeTilOgMed,
                    Ansogningsdato = source.Ansogningsdato

            WHEN NOT MATCHED THEN
                INSERT (
                    Id, Firmanavn, Adresse, CVR, Att, Geo,
                    Serveringszone, Lokation, LokationOptionId,
                    Serveringsareal, Facadelaengde,
                    GaeldendeFra, GaeldendeTilOgMed,
                    Ansogningsdato
                )
                VALUES (
                    source.Id, source.Firmanavn, source.Adresse, source.CVR, source.Att, source.Geo,
                    source.Serveringszone, source.Lokation, source.LokationOptionId,
                    source.Serveringsareal, source.Facadelaengde,
                    source.GaeldendeFra, source.GaeldendeTilOgMed,
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
                ansogningsdato_sql,
            ),
        )

    conn.commit()
    orchestrator_connection.log_info("Application upsert complete.")

    # -------------------------------
    # Generate fakturalinjer
    # -------------------------------
    now_cph = datetime.now(ZoneInfo("Europe/Copenhagen"))

    # Look-ahead horizon: current month + MONTHS_AHEAD - 1 future months,
    # i.e. MONTHS_AHEAD months in total counting the current one.
    horizon_year, horizon_month = add_months(now_cph.year, now_cph.month, MONTHS_AHEAD - 1)

    orchestrator_connection.log_info("Fetching application rows...")
    cursor.execute(
        """
        SELECT
            Id AS DeskproID,
            Firmanavn,
            Adresse,
            CVR,
            Att,
            Geo,
            Serveringszone,
            Lokation,
            LokationOptionId,
            Serveringsareal,
            Facadelaengde,
            GaeldendeFra,
            GaeldendeTilOgMed,
            Ansogningsdato
        FROM dbo.BrugAarhus_Udeservering;
        """
    )
    applications = cursor.fetchall()

    inserted_count = 0
    updated_count = 0
    skipped_locked = 0
    skipped_invalid = 0

    for row in applications:
        deskpro_id = row.DeskproID

        firmanavn = row.Firmanavn
        adresse = row.Adresse
        cvr = row.CVR
        att = row.Att
        geo = row.Geo
        serveringszone = row.Serveringszone
        lokation = row.Lokation
        lokation_option_id = row.LokationOptionId

        base_areal = row.Serveringsareal
        facadelaengde = row.Facadelaengde

        gaeldende_fra = ensure_date(row.GaeldendeFra)
        gaeldende_til = ensure_date(row.GaeldendeTilOgMed)

        if not gaeldende_fra:
            skipped_invalid += 1
            continue

        # Generation window: from gaeldende_fra to min(gaeldende_til, horizon).
        # - All past months back to gaeldende_fra are generated (handles retroactive billing).
        # - Future months only up to MONTHS_AHEAD ahead.
        # - A tidsbegrænset slutdato shortens the window further if it's closer.
        gen_from = (gaeldende_fra.year, gaeldende_fra.month)
        gen_to = (horizon_year, horizon_month)

        if gaeldende_til:
            til_pair = (gaeldende_til.year, gaeldende_til.month)
            if til_pair < gen_to:
                gen_to = til_pair

        if gen_to < gen_from:
            continue

        # Areal: parklet has no areal (charged differently). Else use base_areal.
        faktura_areal = None if lokation_option_id == OPT_LOKATION_PARKLET else base_areal

        for (y, m) in iter_year_months(gen_from, gen_to):
            month_name = MONTH_NUM_TO_NAME[m]
            faktura_date_sort = datetime(y, m, 1)

            # Check existing line + its lock state.
            # - Ny: refresh updates base data so sagsbehandler sees the latest from Deskpro.
            # - TilFakturering / Faktureret / FakturerIkke: locked, never touched.
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
                            lokation,
                            faktura_areal,
                            facadelaengde,
                            row.Ansogningsdato,
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
                    lokation,
                    faktura_areal,
                    facadelaengde,
                    row.Ansogningsdato,
                ),
            )
            inserted_count += 1

    conn.commit()

    orchestrator_connection.log_info(
        "Fakturalinje generation complete. "
        f"Inserted: {inserted_count}, "
        f"Updated Ny: {updated_count}, "
        f"Skipped (locked): {skipped_locked}, "
        f"Skipped (invalid): {skipped_invalid}"
    )

    cursor.close()
    conn.close()


# -----------------------------
# HELPERS
# -----------------------------
def add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    """Return (year, month) `delta` months after the given (year, month)."""
    idx = (year * 12 + (month - 1)) + delta
    return (idx // 12, idx % 12 + 1)


def iter_year_months(start: tuple[int, int], end: tuple[int, int]):
    """Yield (year, month) tuples from start to end inclusive."""
    y, m = start
    ey, em = end
    while (y, m) <= (ey, em):
        yield (y, m)
        m += 1
        if m > 12:
            m = 1
            y += 1


def safe_get_value(field_dict: dict, key: str, default=None):
    """Safely get field_dict[key]['value']."""
    try:
        return field_dict[key].get("value", default)
    except Exception:
        return default


def safe_get_first_detail_title(field_dict: dict, key: str, default=None):
    """Return first title in .detail (dict style) if exists."""
    try:
        detail = field_dict[key].get("detail", {})
        if isinstance(detail, dict) and detail:
            return list(detail.values())[0].get("title", default)
    except Exception:
        pass
    return default


def safe_get_single_select_id(field_dict: dict, key: str, default=None):
    """Return selected option id for single select fields where value is [id]."""
    try:
        v = field_dict[key].get("value")
        if isinstance(v, list) and v:
            return int(v[0])
    except Exception:
        pass
    return default


def parse_deskpro_dt(raw: str | None) -> datetime | None:
    """Parse Deskpro datetime '2026-01-20T09:04:45+0000' -> naive Copenhagen datetime."""
    if not raw:
        return None
    try:
        dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S%z")
        dt_cph = dt.astimezone(ZoneInfo("Europe/Copenhagen"))
        return dt_cph.replace(tzinfo=None)
    except Exception:
        return None


def parse_deskpro_date(raw: str | None) -> date | None:
    """Parse Deskpro date fields: 'YYYY-MM-DD' OR datetime-like 'YYYY-MM-DDT00:00:00+0000'."""
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

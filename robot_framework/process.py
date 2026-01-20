"""This module contains the main process of the robot."""

from OpenOrchestrator.orchestrator_connection.connection import OrchestratorConnection
from OpenOrchestrator.database.queues import QueueElement
import requests
import pyodbc
import json
from datetime import datetime, date
from zoneinfo import ZoneInfo


# Base fields (same as you already used)
FIELD_FIRMANAVN = "55"
FIELD_ADRESSE = "255"
FIELD_CVR = "1258"
FIELD_GEO = "268"          # may be missing on some
FIELD_ZONE = "1216"        # udeservering_zone (single select)
FIELD_LOKATION = "1192"    # where: facade / torv / parklet (single select)
FIELD_SERVERINGSAREAL = "1196"  # base m2 (only relevant when not varying, and not parklet)
FIELD_FACADELAENGDE = "1210"    # only for facade
FIELD_WORKFLOW = "1147"

# Month selections
FIELD_MONTHS_UDEST = "1197"     # udeservering months (checkbox multi-select)
FIELD_MONTHS_PARKLET = "1305"   # parklet months (checkbox multi-select)

# Areal varies?
FIELD_AREAL_VARIERER = "1276"   # single select
OPT_AREAL_VARIERER_JA = 1277
OPT_AREAL_VARIERER_NEJ = 1278

# NEW: validity dates (YOU MUST SET THESE IDS CORRECTLY)
FIELD_GAELDENDE_FRA = "1291"          # <-- set to your new field id
FIELD_GAELDENDE_TIL_OG_MED = "1292"   # <-- set to your new field id (optional)

# Month option-id -> month name (used ONLY for writing FakturaMaaned in DB)
# Logic is based on option IDs, not titles.
MONTH_OPTION_ID_TO_NAME = {
    # Udeservering months (1198..1209)
    1198: "Januar",
    1199: "Februar",
    1200: "Marts",
    1201: "April",
    1202: "Maj",
    1203: "Juni",
    1204: "Juli",
    1205: "August",
    1206: "September",
    1207: "Oktober",
    1208: "November",
    1209: "December",

    # Parklet months (1306..1310)
    1306: "April",
    1307: "Maj",
    1308: "Juni",
    1309: "Juli",
    1310: "August",
}

MONTH_NAME_TO_NUM = {
    "Januar": 1, "Februar": 2, "Marts": 3, "April": 4,
    "Maj": 5, "Juni": 6, "Juli": 7, "August": 8,
    "September": 9, "Oktober": 10, "November": 11, "December": 12
}


MONTH_OPTION_ID_TO_AREAL_FIELD_ID: dict[int, str] = {
    1198: "1279",  # Jan
    1199: "1280",  # Feb
    1200: "1281",  # Mar
    1201: "1282",  # Apr 
    1202: "1283",  # May
    1203: "1284",  # Jun
    1204: "1285",  # Jul
    1205: "1286",  # Aug
    1206: "1287",  # Sep
    1207: "1288",  # Oct
    1208: "1289",  # Nov
    1209: "1290",  # Dec  
}


# -----------------------------
# PROCESS
# -----------------------------
def process(orchestrator_connection: OrchestratorConnection, queue_element: QueueElement | None = None) -> None:
    orchestrator_connection.log_trace("Running process.")

    brugaarhus_api_cred = orchestrator_connection.get_credential("BrugAarhusAPI")
    base_url = brugaarhus_api_cred.username
    token = brugaarhus_api_cred.password

    api_url = f"{base_url}/api/v2/tickets?ticket_field.1228=1229&count=100"
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

        serveringszone = safe_get_first_detail_title(fields, FIELD_ZONE)

        # location option id (single-select)
        lokation_option_id = safe_get_single_select_id(fields, FIELD_LOKATION)
        lokation_title = safe_get_first_detail_title(fields, FIELD_LOKATION)  # stored for readability only

        # facade length only when present
        facadelaengde = safe_get_value(fields, FIELD_FACADELAENGDE)

        # base areal (may be missing for parklet)
        serveringsareal = safe_get_value(fields, FIELD_SERVERINGSAREAL)

        # areal varies?
        areal_varierer_opt = safe_get_single_select_id(fields, FIELD_AREAL_VARIERER)
        areal_varierer = 1 if areal_varierer_opt == OPT_AREAL_VARIERER_JA else 0

        # ticket created date (keep as Ansøgningsdato in DB)
        raw_created = ticket.get("date_created")
        ansogningsdato = parse_deskpro_dt(raw_created)

        # REQUIRED validity window
        gaeldende_fra = parse_deskpro_date(safe_get_value(fields, FIELD_GAELDENDE_FRA))
        gaeldende_til = parse_deskpro_date(safe_get_value(fields, FIELD_GAELDENDE_TIL_OG_MED))

        # Month selections:
        # If parklet months field exists -> use that; else use udeservering months.
        months_option_ids: list[int] = []
        if FIELD_MONTHS_PARKLET in fields and safe_get_multi_select_ids(fields, FIELD_MONTHS_PARKLET):
            months_option_ids = safe_get_multi_select_ids(fields, FIELD_MONTHS_PARKLET)
        else:
            months_option_ids = safe_get_multi_select_ids(fields, FIELD_MONTHS_UDEST)

        months_json = json.dumps(months_option_ids, ensure_ascii=False)

        # Store per-month areal values (only if varying)
        month_areal_map: dict[str, str] = {}
        if areal_varierer == 1:
            for opt_id in months_option_ids:
                areal_field_id = MONTH_OPTION_ID_TO_AREAL_FIELD_ID.get(opt_id)
                if not areal_field_id:
                    continue
                v = safe_get_value(fields, areal_field_id)
                if v is not None and str(v).strip() != "":
                    month_areal_map[str(opt_id)] = str(v).strip()
        month_areal_json = json.dumps(month_areal_map, ensure_ascii=False)
        ansogningsdato_sql = ansogningsdato.strftime("%Y-%m-%d %H:%M:%S") if ansogningsdato else None
        gaeldende_fra_sql = gaeldende_fra.strftime("%Y-%m-%d") if gaeldende_fra else None
        gaeldende_til_sql = gaeldende_til.strftime("%Y-%m-%d") if gaeldende_til else None
        cursor.execute(
            """
            MERGE [dbo].[BrugAarhus_Udeservering] AS target
            USING (
                SELECT
                    ? AS Id,
                    ? AS Firmanavn,
                    ? AS Adresse,
                    ? AS CVR,
                    ? AS Geo,
                    ? AS Serveringszone,
                    ? AS Lokation,
                    ? AS LokationOptionId,
                    ? AS Serveringsareal,
                    ? AS Facadelaengde,
                    ? AS MaanederJson,
                    ? AS ArealVarierer,
                    ? AS MonthArealJson,
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
                    Geo = source.Geo,
                    Serveringszone = source.Serveringszone,
                    Lokation = source.Lokation,
                    LokationOptionId = source.LokationOptionId,
                    Serveringsareal = source.Serveringsareal,
                    Facadelaengde = source.Facadelaengde,
                    MaanederJson = source.MaanederJson,
                    ArealVarierer = source.ArealVarierer,
                    MonthArealJson = source.MonthArealJson,
                    GaeldendeFra = source.GaeldendeFra,
                    GaeldendeTilOgMed = source.GaeldendeTilOgMed,
                    Ansogningsdato = source.Ansogningsdato

            WHEN NOT MATCHED THEN
                INSERT (
                    Id, Firmanavn, Adresse, CVR, Geo, Serveringszone, Lokation, LokationOptionId,
                    Serveringsareal, Facadelaengde,
                    MaanederJson, ArealVarierer, MonthArealJson,
                    GaeldendeFra, GaeldendeTilOgMed,
                    Ansogningsdato
                )
                VALUES (
                    source.Id, source.Firmanavn, source.Adresse, source.CVR, source.Geo, source.Serveringszone, source.Lokation, source.LokationOptionId,
                    source.Serveringsareal, source.Facadelaengde,
                    source.MaanederJson, source.ArealVarierer, source.MonthArealJson,
                    source.GaeldendeFra, source.GaeldendeTilOgMed,
                    source.Ansogningsdato
                );
            """,
            (
                deskpro_id,
                firmanavn,
                adresse,
                cvr,
                geo,
                serveringszone,
                lokation_title,
                lokation_option_id,
                serveringsareal,
                facadelaengde,
                months_json,
                areal_varierer,
                month_areal_json,
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
    current_year = now_cph.year

    orchestrator_connection.log_info("Fetching application rows...")
    cursor.execute(
        """
        SELECT
            Id AS DeskproID,
            Firmanavn,
            Adresse,
            CVR,
            Geo,
            Serveringszone,
            Lokation,
            LokationOptionId,
            Serveringsareal,
            Facadelaengde,
            MaanederJson,
            ArealVarierer,
            MonthArealJson,
            GaeldendeFra,
            GaeldendeTilOgMed,
            Ansogningsdato
        FROM dbo.BrugAarhus_Udeservering;
        """
    )
    applications = cursor.fetchall()

    inserted_count = 0
    skipped_existing = 0
    skipped_invalid = 0

    for row in applications:
        deskpro_id = row.DeskproID

        firmanavn = row.Firmanavn
        adresse = row.Adresse
        cvr = row.CVR
        geo = row.Geo
        serveringszone = row.Serveringszone
        lokation = row.Lokation
        lokation_option_id = row.LokationOptionId

        base_areal = row.Serveringsareal
        facadelaengde = row.Facadelaengde

        areal_varierer = int(row.ArealVarierer or 0)

        gaeldende_fra = ensure_date(row.GaeldendeFra)        # required
        gaeldende_til = ensure_date(row.GaeldendeTilOgMed)   # optional


        # Years to generate:
        # - from gaeldende_fra.year up to current year
        # - and if December, also next year
        start_year = gaeldende_fra.year
        end_year = current_year + (1 if now_cph.month == 12 else 0)

        # Respect gaeldende_til if set
        if gaeldende_til:
            end_year = min(end_year, gaeldende_til.year)

        years_to_generate = range(start_year, end_year + 1)

        # Parse months option IDs
        try:
            month_option_ids = json.loads(row.MaanederJson) if row.MaanederJson else []
            if not isinstance(month_option_ids, list):
                month_option_ids = []
                skipped_invalid += 1
        except Exception:
            month_option_ids = []
            skipped_invalid += 1

        # Parse month areal map
        month_areal_map: dict[str, str] = {}
        try:
            month_areal_map = json.loads(row.MonthArealJson) if row.MonthArealJson else {}
            if not isinstance(month_areal_map, dict):
                month_areal_map = {}
                skipped_invalid += 1
        except Exception:
            month_areal_map = {}
            skipped_invalid += 1

        # Generate invoice lines for selected months in the chosen years
        for opt_id in month_option_ids:
            month_name = MONTH_OPTION_ID_TO_NAME.get(int(opt_id))
            if not month_name:
                skipped_invalid += 1
                continue

            month_num = MONTH_NAME_TO_NUM.get(month_name)
            if not month_num:
                skipped_invalid += 1
                continue

            for y in years_to_generate:
                faktura_date_sort = datetime(y, month_num, 1)
                invoice_month = date(y, month_num, 1)

                # Whole-month billing window check
                if ym(invoice_month) < ym(gaeldende_fra):
                    continue
                if gaeldende_til and ym(invoice_month) > ym(gaeldende_til):
                    continue

                # Compute areal for this line:
                # - Parklet: always NULL
                # - If varies: month-specific if present else fallback to base_areal
                # - Else: base_areal
                faktura_areal = None
                if lokation_option_id != 1195:
                    if areal_varierer == 1:
                        faktura_areal = month_areal_map.get(str(opt_id)) or base_areal
                    else:
                        faktura_areal = base_areal

                # Check if line exists already
                cursor.execute(
                    """
                    SELECT 1
                    FROM dbo.BrugAarhus_Udeservering_Fakturalinjer
                    WHERE DeskproID = ?
                      AND FakturaMaaned = ?
                      AND FakturaAar = ?;
                    """,
                    (deskpro_id, month_name, y),
                )
                if cursor.fetchone():
                    skipped_existing += 1
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
                        Geo,
                        Serveringszone,
                        Lokation,
                        Serveringsareal,
                        Facadelaengde,
                        Periodetype,
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
                        geo,
                        serveringszone,
                        lokation,
                        faktura_areal,
                        facadelaengde,
                        None,
                        row.Ansogningsdato,
                    ),
                )
                inserted_count += 1

    conn.commit()

    orchestrator_connection.log_info(
        "Fakturalinje generation complete. "
        f"Inserted: {inserted_count}, "
        f"Skipped (existing): {skipped_existing}, "
        f"Skipped (invalid JSON/ids): {skipped_invalid}"
    )

    cursor.close()
    conn.close()


# -----------------------------
# HELPERS
# -----------------------------
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
        # Sometimes detail can be dict (select fields), sometimes list (attachments)
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


def safe_get_multi_select_ids(field_dict: dict, key: str) -> list[int]:
    """Return selected option ids for multi-select checkbox fields where value is [id, id, ...]."""
    try:
        v = field_dict[key].get("value")
        if isinstance(v, list):
            return [int(x) for x in v if str(x).isdigit() or isinstance(x, int)]
    except Exception:
        pass
    return []


def parse_deskpro_dt(raw: str | None) -> datetime | None:
    """Parse Deskpro datetime '2026-01-20T09:04:45+0000' -> naive Copenhagen datetime (no tzinfo)."""
    if not raw:
        return None
    try:
        dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S%z")
        dt_cph = dt.astimezone(ZoneInfo("Europe/Copenhagen"))
        return dt_cph.replace(tzinfo=None)  # <-- key fix for old ODBC driver
    except Exception:
        return None



def ym(d: date) -> tuple[int, int]:
    return (d.year, d.month)

def parse_deskpro_date(raw: str | None) -> date | None:
    """Parse Deskpro date fields: 'YYYY-MM-DD' OR datetime-like 'YYYY-MM-DDT00:00:00+0000'."""
    if not raw:
        return None
    try:
        # Most common for Deskpro "date" widget
        if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
            return datetime.strptime(raw, "%Y-%m-%d").date()

        # Fallback: datetime string
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
        # 'YYYY-MM-DD' (most common)
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except Exception:
            return None
    return None


def ensure_datetime(val) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, date) and not isinstance(val, datetime):
        return datetime(val.year, val.month, val.day)
    if isinstance(val, str):
        s = val.strip()
        # Try 'YYYY-MM-DD HH:MM:SS'
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(s[:19], fmt)
            except Exception:
                pass
        # Fallback: just date
        try:
            d = datetime.strptime(s[:10], "%Y-%m-%d").date()
            return datetime(d.year, d.month, d.day)
        except Exception:
            return None
    return None

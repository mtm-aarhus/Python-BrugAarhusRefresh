"""This module contains the main process of the robot."""

from OpenOrchestrator.orchestrator_connection.connection import OrchestratorConnection
from OpenOrchestrator.database.queues import QueueElement
import requests
import pyodbc
import json
from datetime import datetime
from zoneinfo import ZoneInfo

# pylint: disable-next=unused-argument
def process(orchestrator_connection: OrchestratorConnection, queue_element: QueueElement | None = None) -> None:
    orchestrator_connection.log_trace("Running process.")

    brugaarhus_api_cred = orchestrator_connection.get_credential("BrugAarhusAPI")
    base_url = brugaarhus_api_cred.username
    token = brugaarhus_api_cred.password

    API_URL = f"{base_url}/api/v2/tickets?ticket_field.1228=1229&count=10"
    HEADERS = {
        "Authorization": token,
        "Cookie": "dp_last_lang=da"
    }

    # SQL Connection
    sql_server = orchestrator_connection.get_constant("SqlServer")
    conn_string = (
        f"DRIVER={{SQL Server}};"
        f"SERVER={sql_server.value};"
        f"DATABASE=PYORCHESTRATOR;"
        f"Trusted_Connection=yes;"
    )
    conn = pyodbc.connect(conn_string)
    cursor = conn.cursor()

    # Fetch all pages of API data
    all_data = []
    page = 1
    while True:
        resp = requests.get(f"{API_URL}&page={page}", headers=HEADERS)
        if resp.status_code != 200:
            break

        payload = resp.json()
        data = payload.get("data", [])
        meta = payload.get("meta", {})
        pagination = meta.get("pagination", {})

        if not data:
            break

        all_data.extend(data)

        if pagination.get("current_page") >= pagination.get("total_pages"):
            break
        page += 1

    # Process tickets
    for ticket in all_data:
        fields = ticket.get("fields", {})

        Id = ticket.get("id")
        Firmanavn = safe_get(fields, "55")
        Adresse = safe_get(fields, "255")
        CVR = safe_get(fields, "1258")
        Geo = safe_get(fields, "268")
        Serveringszone = safe_get_detail_title(fields, "1216")
        Lokation = extract_single_select_title(fields, "1192")
        Serveringsareal = safe_get(fields, "1196")
        Facadelaengde = safe_get(fields, "1210") 
        Periodetype = extract_single_select_title(fields, "1272")
        MaanederFremtidige = extract_month_list(fields, "1197")
        MaanederIndevaerende = extract_month_list(fields, "1259")
        raw_date = ticket.get("date_created")
        Ansogningsdato = datetime.strptime(raw_date, "%Y-%m-%dT%H:%M:%S%z")
        Ansogningsdato = Ansogningsdato.astimezone(ZoneInfo("Europe/Copenhagen"))

        cursor.execute("""
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
                ? AS Serveringsareal,
                ? AS Facadelaengde,
                ? AS Periodetype,
                ? AS MaanederIndevaerende,
                ? AS MaanederFremtidige,
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
                Serveringsareal = source.Serveringsareal,
                Facadelaengde = source.Facadelaengde,
                Periodetype = source.Periodetype,
                MaanederIndevaerende = source.MaanederIndevaerende,
                MaanederFremtidige = source.MaanederFremtidige,
                Ansogningsdato = source.Ansogningsdato

        WHEN NOT MATCHED THEN
            INSERT (
                Id,
                Firmanavn,
                Adresse,
                CVR,
                Geo,
                Serveringszone,
                Lokation,
                Serveringsareal,
                Facadelaengde,
                Periodetype,
                MaanederIndevaerende,
                MaanederFremtidige,
                Ansogningsdato
            )
            VALUES (
                source.Id,
                source.Firmanavn,
                source.Adresse,
                source.CVR,
                source.Geo,
                source.Serveringszone,
                source.Lokation,
                source.Serveringsareal,
                source.Facadelaengde,
                source.Periodetype,
                source.MaanederIndevaerende,
                source.MaanederFremtidige,
                source.Ansogningsdato
            );
    """, (
        Id,
        Firmanavn,
        Adresse,
        CVR,
        Geo,
        Serveringszone,
        Lokation,
        Serveringsareal,
        Facadelaengde,
        Periodetype,
        MaanederIndevaerende,
        MaanederFremtidige,
        Ansogningsdato
    ))

    conn.commit()

    
    now = datetime.now()
    current_year = now.year

    MONTH_ORDER = {
    "Januar": 1, "Februar": 2, "Marts": 3, "April": 4,
    "Maj": 5, "Juni": 6, "Juli": 7, "August": 8,
    "September": 9, "Oktober": 10, "November": 11, "December": 12
    }

    orchestrator_connection.log_info("Fetching application rows...")

    cursor.execute("""
        SELECT 
            Id AS DeskproID,
            Firmanavn,
            Adresse,
            CVR,
            Geo,
            Serveringszone,
            Lokation,
            Serveringsareal,
            Facadelaengde,
            Periodetype,
            MaanederIndevaerende,
            MaanederFremtidige,
            Ansogningsdato
        FROM dbo.BrugAarhus_Udeservering;
    """)

    applications = cursor.fetchall()

    inserted_count = 0
    skipped_existing = 0
    skipped_invalid = 0

    for row in applications:
        DeskproID = row.DeskproID
        Firmanavn = row.Firmanavn
        Adresse = row.Adresse
        CVR = row.CVR
        Geo = row.Geo
        Serveringszone = row.Serveringszone
        Lokation = row.Lokation
        Serveringsareal = row.Serveringsareal
        Facadelaengde = row.Facadelaengde
        Periodetype = row.Periodetype
        Ansogningsdato = row.Ansogningsdato

        # -------------------------------
        # Parse month lists safely
        # -------------------------------
        try:
            months_current = json.loads(row.MaanederIndevaerende) if row.MaanederIndevaerende else []
        except:
            months_current = []
            skipped_invalid += 1

        try:
            months_future = json.loads(row.MaanederFremtidige) if row.MaanederFremtidige else []
        except:
            months_future = []
            skipped_invalid += 1

        # -------------------------------
        # Expand into faktureringslinie months
        # -------------------------------

        # Determine the year based on application date
        base_year = Ansogningsdato.year

        # Expand into faktureringslinie months
        faktura_months = []

        # Indeværende år months get year = base_year
        for m in months_current:
            faktura_months.append((m, base_year))

        # Fremtidige år months get year = base_year + 1
        for m in months_future:
            faktura_months.append((m, current_year + 1))

        # -------------------------------
        # Insert missing fakturalinjer
        # -------------------------------
        for month_name, year_value in faktura_months:
            # Month number for sortable date

            # Check if line exists already
            cursor.execute("""
                SELECT 1
                FROM dbo.BrugAarhus_Udeservering_Fakturalinjer
                WHERE DeskproID = ?
                  AND FakturaMaaned = ?
                  AND FakturaAar = ?;
            """, (DeskproID, month_name, year_value))

            exists = cursor.fetchone()

            if exists:
                skipped_existing += 1
                continue  # Do NOT modify existing rows

            month_num = MONTH_ORDER.get(month_name, 1)
            faktura_date_sort = datetime(year_value, month_num, 1)

            # Insert fakturalinje
            cursor.execute("""
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
            """, (
                DeskproID,
                month_name,
                year_value,
                faktura_date_sort, 
                Firmanavn,
                Adresse,
                CVR,
                Geo,
                Serveringszone,
                Lokation,
                Serveringsareal,
                Facadelaengde,
                Periodetype,
                Ansogningsdato
            ))

            inserted_count += 1

    conn.commit()

    orchestrator_connection.log_info(
        f"Fakturalinje generation complete. "
        f"Inserted: {inserted_count}, "
        f"Skipped (existing): {skipped_existing}, "
        f"Skipped (invalid JSON): {skipped_invalid}"
    )


# -----------------------------
# HELPER FUNCTIONS
# -----------------------------
def safe_get(field_dict, key, nested="value", default=None):
    """Safely get nested field value"""
    try:
        return field_dict[key].get(nested, default)
    except Exception:
        return default

def safe_get_detail_title(field_dict, key, default=None):
    """Return first title in .detail if exists"""
    try:
        detail = field_dict[key].get("detail", {})
        if isinstance(detail, dict) and len(detail) > 0:
            return list(detail.values())[0].get("title", default)
    except Exception:
        pass
    return default



def extract_month_list(field_dict, key):
    """Return JSON array of month titles for a multi-select month field."""
    try:
        detail = field_dict[key].get("detail", {})
        return json.dumps([v["title"] for v in detail.values()])
    except Exception:
        return None


def extract_single_select_title(field_dict, key):
    """Return 'title' for a single-select Deskpro field (value = [id])"""
    try:
        detail = field_dict[key].get("detail", {})
        if len(detail) > 0:
            return list(detail.values())[0].get("title")
    except Exception:
        pass
    return None

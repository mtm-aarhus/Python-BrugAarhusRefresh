# BrugAarhus – Deskpro Invoice Synchronization Robot

## Overview

This process is an automated robot that synchronizes invoice-related data from the Deskpro API into two SQL Server tables used by the BrugAarhus invoicing system (“BrugAarhusKassen”).

The robot is triggered inside **OpenOrchestrator**, fetches all Deskpro tickets that have been marked as ready for invoicing (`ticket_field.1228 = 1229`), updates local application rows, and generates any missing invoice lines.

Its purpose is to ensure that BrugAarhus’ invoicing foundation is always fresh, consistent, and complete – even when tickets are edited or backfilled in Deskpro.

---

## High-Level Workflow

1. **Authenticate to Deskpro API**

   * The robot reads a stored credential named `BrugAarhusAPI` from Orchestrator.
   * The credential exposes:

     * `username` → API base URL
     * `password` → API token
   * Calls the Deskpro endpoint:

     ```
     /api/v2/tickets?ticket_field.1228=1229&count=10
     ```

     and automatically iterates through *all pages*.

2. **SQL Server Connection**

   * Establishes a trusted connection to the SQL Server using the Orchestrator constant `SqlServer`.
   * Works against the database `PYORCHESTRATOR`.

3. **Fetch and Process All Relevant Tickets**
   For each ticket returned from Deskpro:

   * Extracts all relevant fields (company name, address, CVR, serving zone, geo, location type, serving area, façade length, selected months, etc.).
   * Converts date fields to Copenhagen timezone.
   * Safely parses Deskpro’s field structures using helper functions.

4. **Upsert Application Records** (Ansøgninger)
   Each Deskpro ticket is merged into the SQL table:

   ```
   dbo.BrugAarhus_Udeservering
   ```

   The MERGE statement ensures:

   * Existing rows are updated.
   * New rows are inserted.
   * No duplicates are created.

5. **Generate Missing Invoice Lines**
   After all application rows have been updated, the robot:

   * Reads **all** applications from `BrugAarhus_Udeservering`.
   * Parses the month lists for:

     * `MaanederIndevaerende` (current year)
     * `MaanederFremtidige` (next year)
   * Determines invoice months and their correct year.
   * Inserts invoice lines into:

     ```
     dbo.BrugAarhus_Udeservering_Fakturalinjer
     ```

     but **only if they do not already exist**.

   Each generated invoicing line contains:

   * Deskpro ticket ID
   * Invoice month + year
   * Sortable date (`FakturaDatoSort`)
   * Company and application details
   * Application date
   * Default `FakturaStatus = 'Ny'`

6. **Logging and Completion**

   * Logs informational messages and counts:

     * Inserted invoice lines
     * Skipped due to existing rows
     * Skipped due to invalid month JSON
   * Commits all SQL transactions.

---

## Field Mapping Summary

The robot extracts a variety of Deskpro custom fields, including (examples):

* `55` → Firmanavn
* `255` → Adresse
* `1258` → CVR
* `268` → Geo
* `1216` → Serveringszone (detailed title)
* `1192` → Lokation (single select)
* `1196` → Serveringsareal
* `1210` → Facadelaengde
* `1272` → Periodetype
* `1197` → MaanederFremtidige (month multi-select)
* `1259` → MaanederIndevaerende (month multi-select)

---

## What the Robot Guarantees

✔ BrugAarhus_Udeservering always reflects the newest Deskpro ticket data
✔ Missing invoice lines are auto-generated for all relevant months
✔ No duplicate invoice lines will ever be created
✔ Deskpro pagination is fully handled
✔ Timezones are correctly converted to Europe/Copenhagen

---

## Known Limitations / Assumptions

* Deskpro field structures must remain consistent with the ones used today.
* Month lists must be valid JSON lists of titles; invalid JSON month lists are skipped.
* The robot does **not** delete or modify existing invoice lines—only inserts missing ones.
* Only tickets with the specified deskpro field (`ticket_field.1228=1229`) are processed.
* All SQL operations assume the structure of BrugAarhus tables is unchanged.

---

## Helper Functions

The script includes multiple safety-oriented helper functions:

* `safe_get()` – safely extract nested values
* `safe_get_detail_title()` – extract `.detail` titles from Deskpro fields
* `extract_single_select_title()` – extract selected title for single-select fields
* `extract_month_list()` – convert multi-select detail maps into JSON arrays

These ensure robust handling of Deskpro’s complex field payloads.

---

# Robot-Framework V3

This repo is meant to be used as a template for robots made for [OpenOrchestrator](https://github.com/itk-dev-rpa/OpenOrchestrator).

## Quick start

1. To use this template simply use this repo as a template (see [Creating a repository from a template](https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-repository-from-a-template)).
__Don't__ include all branches.

2. Go to `robot_framework/__main__.py` and choose between the linear framework or queue based framework.

3. Implement all functions in the files:
    * `robot_framework/initialize.py`
    * `robot_framework/reset.py`
    * `robot_framework/process.py`

4. Change `config.py` to your needs.

5. Fill out the dependencies in the `pyproject.toml` file with all packages needed by the robot.

6. Feel free to add more files as needed. Remember that any additional python files must
be located in the folder `robot_framework` or a subfolder of it.

When the robot is run from OpenOrchestrator the `main.py` file is run which results
in the following:
1. The working directory is changed to where `main.py` is located.
2. A virtual environment is automatically setup with the required packages.
3. The framework is called passing on all arguments needed by [OpenOrchestrator](https://github.com/itk-dev-rpa/OpenOrchestrator).

## Requirements
Minimum python version 3.10

## Flow

This framework contains two different flows: A linear and a queue based.
You should only ever use one at a time. You choose which one by going into `robot_framework/__main__.py`
and uncommenting the framework you want. They are both disabled by default and an error will be
raised to remind you if you don't choose.

### Linear Flow

The linear framework is used when a robot is just going from A to Z without fetching jobs from an
OpenOrchestrator queue.
The flow of the linear framework is sketched up in the following illustration:

![Linear Flow diagram](Robot-Framework.svg)

### Queue Flow

The queue framework is used when the robot is doing multiple bite-sized tasks defined in an
OpenOrchestrator queue.
The flow of the queue framework is sketched up in the following illustration:

![Queue Flow diagram](Robot-Queue-Framework.svg)

## Linting and Github Actions

This template is also setup with flake8 and pylint linting in Github Actions.
This workflow will trigger whenever you push your code to Github.
The workflow is defined under `.github/workflows/Linting.yml`.


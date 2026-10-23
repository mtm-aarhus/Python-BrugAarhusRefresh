"""What a sync changed in BrugAarhus_Udeservering — for kassen's message when
the sync is done ("2 nye tilladelser, 1 ændret: Café X (m², slutdato)").

Kept apart from process.py (no OpenOrchestrator / database imports) so it can
be tested on its own.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

# MERGE_SQL's columns, in the order of tilladelse_params().
KOLONNER = (
    "Id", "Firmanavn", "Adresse", "CVR", "Att", "Serveringszone",
    "Udeserveringstype", "UdeserveringstypeOptionId", "Serveringsareal",
    "Facadelaengde", "GaeldendeFra", "GaeldendeTilOgMed", "Sommersaeson",
    "Vintermaaneder", "Workflow", "WorkflowOptionId", "Ansogningsdato",
)

# The changes worth telling a sagsbehandler about, with their Danish names.
VIST = {
    "Firmanavn": "navn", "Adresse": "adresse", "CVR": "CVR", "Att": "att.",
    "Serveringszone": "zone", "Udeserveringstype": "type", "Serveringsareal": "m²",
    "Facadelaengde": "facade", "GaeldendeFra": "startdato", "GaeldendeTilOgMed": "slutdato",
    "Sommersaeson": "sommersæson", "Vintermaaneder": "vintermåneder",
    "Workflow": "status i Deskpro",
}

# Names sent per list; the counts are always complete.
MAKS_NAVNE = 50

_MIDNAT = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T]00:00:00(\.0+)?$")


def norm(value):
    """A value as compared between the database and Deskpro: numbers as
    numbers (20 == 20.00 == "20"), a midnight datetime as its date, text
    trimmed, empty as None."""
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return round(float(value), 4)
    if isinstance(value, datetime):
        value = value.isoformat(sep=" ")
    elif isinstance(value, date):
        value = value.isoformat()
    tekst = str(value).strip()
    if not tekst:
        return None
    midnat = _MIDNAT.match(tekst)
    if midnat:
        return midnat.group(1)
    try:
        return round(float(tekst), 4)
    except ValueError:
        return tekst


def aendrede_felter(foer: dict, params: tuple) -> list[str]:
    """The Danish names of the shown fields that differ between the stored
    row and the new MERGE parameters."""
    ny = dict(zip(KOLONNER, params))
    return [navn for kolonne, navn in VIST.items() if norm(foer.get(kolonne)) != norm(ny.get(kolonne))]


class Aendringer:
    """Collects new and changed tilladelser during a sync."""

    def __init__(self, eksisterende: dict[int, dict]):
        self.eksisterende = eksisterende
        self.nye: list[dict] = []
        self.aendrede: list[dict] = []

    def noter(self, params: tuple) -> None:
        ny = dict(zip(KOLONNER, params))
        foer = self.eksisterende.get(int(ny["Id"]))
        if foer is None:
            self.nye.append({"id": ny["Id"], "navn": ny["Firmanavn"]})
            return
        felter = aendrede_felter(foer, params)
        if felter:
            self.aendrede.append({"id": ny["Id"], "navn": ny["Firmanavn"], "felter": felter})

    def til_kassen(self) -> dict:
        """The part of the "faerdig" report kassen shows."""
        return {
            "nye": len(self.nye),
            "aendrede": len(self.aendrede),
            "nye_liste": self.nye[:MAKS_NAVNE],
            "aendrede_liste": self.aendrede[:MAKS_NAVNE],
        }

"""What a sync counts as new or changed (robot_framework/aendringer.py) —
the robot reads the database with pyodbc (Decimal, date, datetime) and gets
Deskpro's values as numbers and text, so equal values must compare equal."""
import unittest
from datetime import date, datetime
from decimal import Decimal

from robot_framework.aendringer import KOLONNER, Aendringer, aendrede_felter, norm


def params(**felter):
    """MERGE parameters as tilladelse_params() builds them from Deskpro."""
    standard = {
        "Id": 1, "Firmanavn": "Café A", "Adresse": "Mejlgade 1\n8000 Aarhus C", "CVR": 30150007,
        "Att": "Mette Jensen", "Serveringszone": "1", "Udeserveringstype": "Alm. udeservering (fortløbende)",
        "UdeserveringstypeOptionId": 1193, "Serveringsareal": "20", "Facadelaengde": 4,
        "GaeldendeFra": "2026-11-01", "GaeldendeTilOgMed": None, "Sommersaeson": "Ja",
        "Vintermaaneder": None, "Workflow": "Ansøgning godkendt", "WorkflowOptionId": 1151,
        "Ansogningsdato": "2026-03-15 10:00:00",
    }
    standard.update(felter)
    return tuple(standard[k] for k in KOLONNER)


# The same tilladelse as stored and read back through pyodbc.
GEMT = {
    "Id": 1, "Firmanavn": "Café A", "Adresse": "Mejlgade 1\n8000 Aarhus C", "CVR": "30150007",
    "Att": "Mette Jensen", "Serveringszone": "1", "Udeserveringstype": "Alm. udeservering (fortløbende)",
    "UdeserveringstypeOptionId": 1193, "Serveringsareal": Decimal("20.00"), "Facadelaengde": Decimal("4.0"),
    "GaeldendeFra": datetime(2026, 11, 1), "GaeldendeTilOgMed": None, "Sommersaeson": "Ja",
    "Vintermaaneder": "", "Workflow": "Ansøgning godkendt", "WorkflowOptionId": 1151,
    "Ansogningsdato": datetime(2026, 3, 15, 10, 0),
}


class Norm(unittest.TestCase):
    def test_samme_vaerdi_i_forskellig_form(self):
        self.assertEqual(norm(Decimal("20.00")), norm("20"))
        self.assertEqual(norm(datetime(2026, 11, 1)), norm("2026-11-01"))
        self.assertEqual(norm(date(2026, 11, 1)), norm("2026-11-01"))
        self.assertEqual(norm(""), norm(None))
        self.assertEqual(norm(" Ja "), norm("Ja"))

    def test_forskellige_vaerdier(self):
        self.assertNotEqual(norm(Decimal("20")), norm("35"))
        self.assertNotEqual(norm(datetime(2026, 11, 1, 12, 0)), norm("2026-11-01"))


class Felter(unittest.TestCase):
    def test_uaendret_giver_intet(self):
        self.assertEqual(aendrede_felter(GEMT, params()), [])

    def test_navngiver_de_aendrede_felter(self):
        self.assertEqual(
            aendrede_felter(GEMT, params(Serveringsareal=35, GaeldendeTilOgMed="2027-03-31",
                                         Workflow="Ansøgning opsagt", WorkflowOptionId=1341)),
            ["m²", "slutdato", "status i Deskpro"])


class Opsummering(unittest.TestCase):
    def test_nye_og_aendrede(self):
        a = Aendringer({1: GEMT})
        a.noter(params())                                # unchanged
        a.noter(params(Serveringsareal=35))               # changed
        a.noter(params(Id=2, Firmanavn="Bar B"))          # new
        self.assertEqual(a.til_kassen(), {
            "nye": 1, "aendrede": 1,
            "nye_liste": [{"id": 2, "navn": "Bar B"}],
            "aendrede_liste": [{"id": 1, "navn": "Café A", "felter": ["m²"]}],
        })


if __name__ == "__main__":
    unittest.main()

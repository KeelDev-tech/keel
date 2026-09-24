"""Explicitly synthetic, frozen-time workspace; never freshens operational data."""
from tools.make_trust_demo import make_document, NOW
from .model import adapt_body


def make_demo():
    trust = make_document(); leads = trust["flow_export"]["leads"]
    displays = [("Northstar Logistics", "Operations Manager", "Operations"),
                ("Harbor Works", "Customer Success Lead", "Sales"),
                ("Atlas Security", "Account Executive", "Sales"),
                ("Meridian Labs", "Technical Operations Lead", "Technology"),
                ("Stripe · fixture", "Customer Success Manager", "Sales"),
                ("Fieldwork", "Regional Operations Lead", "Operations"),
                ("Northstar Logistics", "Business Development Lead", "Sales"),
                ("Meridian Labs", "AI Operations Specialist", "Technology"),
                ("Harbor Works", "General Manager", "Operations")]
    labels = [{"role_id": row["role_id"], "company": value[0], "title": value[1], "lane": value[2]}
              for row, value in zip(leads, displays)]
    return adapt_body({"flow": trust["flow_export"], "assurance": None, "trust": trust},
                      trust["workspace_id"], synthetic=True, labels=labels)

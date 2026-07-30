import json
from fastapi.testclient import TestClient
from bot import app

client = TestClient(app)

print("healthz", client.get("/v1/healthz").json())
print("metadata", client.get("/v1/metadata").json())

category = {
    "slug": "dentists",
    "display_name": "Dentists",
    "voice": {"tone": "peer_clinical"},
    "offer_catalog": [{"title": "Dental Cleaning @ ₹299", "type": "service_at_price"}],
    "digest": [{"id": "d1", "title": "3-month fluoride recall cuts caries 38% better", "source": "JIDA Oct 2026, p.14", "trial_n": 2100, "patient_segment": "high_risk_adults"}],
}
merchant = {
    "merchant_id": "m1",
    "category_slug": "dentists",
    "identity": {"name": "Dr. Meera's Dental Clinic", "owner_first_name": "Meera", "locality": "Lajpat Nagar", "languages": ["en", "hi"]},
    "performance": {"views": 2410, "calls": 18, "directions": 45, "ctr": 0.021},
    "offers": [{"title": "Dental Cleaning @ ₹299", "status": "active"}],
    "signals": ["high_risk_adult_cohort"],
    "customer_aggregate": {"high_risk_adult_count": 124}
}
trigger = {
    "id": "t1", "scope": "merchant", "kind": "research_digest", "source": "external", "merchant_id": "m1", "customer_id": None,
    "payload": {"top_item_id": "d1"}, "urgency": 2, "suppression_key": "research:test", "expires_at": "2026-05-03T00:00:00Z"
}
for scope, cid, payload in [("category", "dentists", category), ("merchant", "m1", merchant), ("trigger", "t1", trigger)]:
    print(client.post("/v1/context", json={"scope": scope, "context_id": cid, "version": 1, "payload": payload, "delivered_at": "2026-04-26T10:00:00Z"}).json())
print(json.dumps(client.post("/v1/tick", json={"now": "2026-04-26T10:05:00Z", "available_triggers": ["t1"]}).json(), indent=2))

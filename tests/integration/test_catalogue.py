import uuid

import pytest

from tests.helpers import create_booking


def test_anyone_can_list_centres_with_pagination(client, catalogue):
    response = client.get("/centres", params={"limit": 1})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["limit"] == 1
    assert [c["name"] for c in body["items"]] == ["Alpha Labs"]


def test_centre_detail_includes_offered_tests_and_prices(client, catalogue):
    response = client.get(f"/centres/{catalogue.alpha.id}")

    assert response.status_code == 200
    tests = {t["name"]: t["price"] for t in response.json()["tests"]}
    assert tests == {"Complete Blood Count": "500.00", "Lipid Profile": "800.00"}


def test_same_test_can_have_different_prices_at_different_centres(client, catalogue):
    alpha = client.get(f"/centres/{catalogue.alpha.id}/tests").json()
    beta = client.get(f"/centres/{catalogue.beta.id}/tests").json()

    alpha_cbc = next(t for t in alpha if t["test_id"] == str(catalogue.cbc.id))
    beta_cbc = next(t for t in beta if t["test_id"] == str(catalogue.cbc.id))
    assert (alpha_cbc["price"], beta_cbc["price"]) == ("500.00", "450.00")


def test_anyone_can_list_and_get_tests(client, catalogue):
    listed = client.get("/tests")
    single = client.get(f"/tests/{catalogue.cbc.id}")

    assert listed.status_code == 200
    assert listed.json()["total"] == 3
    assert single.status_code == 200
    assert single.json()["name"] == "Complete Blood Count"


def test_unknown_and_malformed_ids(client):
    assert client.get(f"/centres/{uuid.uuid4()}").status_code == 404
    assert client.get(f"/tests/{uuid.uuid4()}").status_code == 404
    assert client.get("/centres/not-a-uuid").status_code == 422


def test_admin_can_manage_catalogue(client, admin_headers):
    centre = client.post(
        "/centres", json={"name": "Gamma Health", "location": "Pune"}, headers=admin_headers
    )
    test = client.post("/tests", json={"name": "HbA1c"}, headers=admin_headers)
    assert centre.status_code == test.status_code == 201
    centre_id, test_id = centre.json()["id"], test.json()["id"]

    offered = client.post(
        f"/centres/{centre_id}/tests",
        json={"test_id": test_id, "price": "650.00"},
        headers=admin_headers,
    )
    assert offered.status_code == 201

    repriced = client.patch(
        f"/centres/{centre_id}/tests/{test_id}", json={"price": "600"}, headers=admin_headers
    )
    assert repriced.json()["price"] == "600.00"

    renamed = client.patch(
        f"/centres/{centre_id}", json={"name": "Gamma Health Plus"}, headers=admin_headers
    )
    assert renamed.json()["name"] == "Gamma Health Plus"
    assert renamed.json()["location"] == "Pune"

    removed = client.delete(f"/centres/{centre_id}/tests/{test_id}", headers=admin_headers)
    assert removed.status_code == 204
    assert client.get(f"/centres/{centre_id}/tests").json() == []

    assert client.delete(f"/centres/{centre_id}", headers=admin_headers).status_code == 204
    assert client.get(f"/centres/{centre_id}").status_code == 404


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/centres", {"name": "X", "location": "Y"}),
        ("post", "/tests", {"name": "X"}),
    ],
)
def test_regular_user_cannot_manage_catalogue(client, user_headers, method, path, body):
    response = client.request(method, path, json=body, headers=user_headers)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_regular_user_cannot_change_prices_or_delete(client, catalogue, user_headers):
    centre, cbc = catalogue.alpha.id, catalogue.cbc.id

    assert (
        client.patch(
            f"/centres/{centre}/tests/{cbc}", json={"price": "1"}, headers=user_headers
        ).status_code
        == 403
    )
    assert client.delete(f"/centres/{centre}", headers=user_headers).status_code == 403
    assert client.get(f"/centres/{centre}").json()["tests"][0]["price"] == "500.00"


def test_anonymous_user_cannot_manage_catalogue(client):
    response = client.post("/centres", json={"name": "X", "location": "Y"})

    assert response.status_code == 401


def test_duplicate_test_name_and_duplicate_offering_are_conflicts(client, catalogue, admin_headers):
    duplicate_test = client.post(
        "/tests", json={"name": "Complete Blood Count"}, headers=admin_headers
    )
    duplicate_offering = client.post(
        f"/centres/{catalogue.alpha.id}/tests",
        json={"test_id": str(catalogue.cbc.id), "price": "999"},
        headers=admin_headers,
    )

    assert duplicate_test.status_code == 409
    assert duplicate_offering.status_code == 409
    assert duplicate_offering.json()["error"]["code"] == "OFFERING_EXISTS"


def test_offering_price_must_be_positive(client, catalogue, admin_headers):
    response = client.post(
        f"/centres/{catalogue.beta.id}/tests",
        json={"test_id": str(catalogue.lipid.id), "price": "0"},
        headers=admin_headers,
    )

    assert response.status_code == 422


def test_patch_cannot_null_out_required_fields(client, catalogue, admin_headers):
    response = client.patch(
        f"/centres/{catalogue.alpha.id}", json={"name": None}, headers=admin_headers
    )

    assert response.status_code == 422


def test_centre_with_bookings_cannot_be_deleted(client, catalogue, admin_headers, user_headers):
    create_booking(client, user_headers, catalogue.alpha.id, catalogue.cbc.id)

    response = client.delete(f"/centres/{catalogue.alpha.id}", headers=admin_headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CENTRE_IN_USE"
    assert client.get(f"/centres/{catalogue.alpha.id}").status_code == 200

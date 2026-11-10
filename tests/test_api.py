# TODO: Implement test cases for the API


def test_api_request(client, database):
    response = client.get("/races")
    assert response is not None


# def test_api_request_tracks(client, database):
#     response = client.get("/races")
#     assert response is not None

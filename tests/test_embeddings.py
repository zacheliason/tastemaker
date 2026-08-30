from listing_agent.embeddings import cosine_similarity


def test_cosine_similarity_ranks_matching_vectors():
    query = [1.0, 0.0]
    assert cosine_similarity(query, [1.0, 0.0]) > cosine_similarity(query, [0.0, 1.0])
